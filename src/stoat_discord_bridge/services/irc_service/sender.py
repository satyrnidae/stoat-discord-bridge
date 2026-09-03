"""`IrcSenderService`: connection lifecycle, channel management, inbound relay.

Instantiated once per configured IRC connector (config.yaml's `irc` list
can have any number of entries). Turns native IRC events into
`StandardMessage`s and owns the shared connection the receiver also posts
through.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable

from stoat_discord_bridge.admin_commands import ChannelLinker, UserLinker
from stoat_discord_bridge.config import IrcConnectorConfig
from stoat_discord_bridge.models import ChannelMetadata, StandardMessage
from stoat_discord_bridge.services.base import OnMessage, SenderService
from stoat_discord_bridge.services.irc_service.client import _IrcClient
from stoat_discord_bridge.services.irc_service.commands import (
    _ADMIN_DM_CHANNEL_VERBS,
    _ADMIN_DM_TWO_WORD_NOUNS,
    _HELP_TEXT,
    IrcAdminCommandsMixin,
)
from stoat_discord_bridge.services.irc_service.formatting import (
    _HISTORY_REPLAY_NOTICE_RE,
    _HISTORY_REPLAY_TIMEOUT,
    _PERMANENT_CHANNEL_MODE,
    _split_permanent_mode,
    _synthetic_message_id,
    normalize_channel_name,
)
from stoat_discord_bridge.status import HealthTracker

logger = logging.getLogger(__name__)


class IrcSenderService(IrcAdminCommandsMixin, SenderService):
    def __init__(
        self,
        config: IrcConnectorConfig,
        channels: list[str],
        on_message: OnMessage,
        health: HealthTracker,
        linker: ChannelLinker | None = None,
        user_linker: UserLinker | None = None,
    ) -> None:
        SenderService.__init__(self, on_message)
        self._config = config
        self.connector_id = config.id
        self._channels = list(channels)
        self._health = health
        self._linker = linker
        self._user_linker = user_linker
        self._client = _IrcClient(self, config)
        self._loop: asyncio.AbstractEventLoop | None = None
        # Pending WHOIS queries issued by _check_is_oper, keyed by lowercased
        # nick, awaiting resolution from the reactor thread's on_whoisoperator
        # / on_endofwhois / on_nosuchnick callbacks - see _resolve_whois.
        self._pending_whois: dict[str, asyncio.Future] = {}
        # Channel (lowercased) -> (remaining count, deadline) for a history
        # replay currently in progress - see _handle_pubnotice/
        # _consume_history_replay and _HISTORY_REPLAY_NOTICE_RE's comment.
        self._history_replay: dict[str, tuple[int, float]] = {}
        # Channels a JOIN was rejected for (ERR_NEEDREGGEDNICK/477 - this
        # network requires a registered+identified nick to join anything but
        # #welcome) and hasn't yet been retried - see _handle_join_blocked/
        # _retry_blocked_joins. Without this, a JOIN sent immediately on
        # connect (before NickServ's IDENTIFY reply comes back) is silently
        # dropped by the server and never retried, so the bridge looks
        # "connected" while never actually being in the channel.
        self._blocked_channels: set[str] = set()
        # Set once the server confirms our OPER (on_youreoper). Gates
        # _PERMANENT_CHANNEL_MODE, which is oper-only.
        self._is_oper = False
        # Synced channels created before our OPER was confirmed, awaiting
        # +P once it is - see _apply_permanent_mode/_handle_youreoper.
        # Touched from both the asyncio thread (_apply_permanent_mode, via
        # join_channel) and the reactor thread (_handle_youreoper); set
        # operations are individually atomic under the GIL and a lost/
        # doubled entry only costs a missed or repeated harmless MODE.
        self._pending_permanent_modes: set[str] = set()

    @property
    def connection(self):
        return self._client.connection

    def _handle_welcome(self, connection) -> None:
        self._health.mark_connected(self.connector_id)
        logger.info("[irc:%s] connected as %s (%s:%s)", self.connector_id, self._config.nick, self._config.host, self._config.port)
        if self._config.nickserv_password:
            connection.privmsg("NickServ", f"IDENTIFY {self._config.nickserv_password}")
        if self._config.oper_account and self._config.oper_password:
            # OPER's login name is deliberately not required to match `ident`
            # or `nick` - see IrcConnectorConfig.oper_account.
            connection.oper(self._config.oper_account, self._config.oper_password)
        for channel in self._channels:
            connection.join(channel)

    def _handle_youreoper(self) -> None:
        self._is_oper = True
        if not self._pending_permanent_modes:
            logger.info("[irc:%s] OPER confirmed", self.connector_id)
            return
        pending = sorted(self._pending_permanent_modes)
        self._pending_permanent_modes.clear()
        logger.info("[irc:%s] OPER confirmed - applying %s to %s", self.connector_id, _PERMANENT_CHANNEL_MODE, ", ".join(pending))
        for channel in pending:
            self.connection.mode(channel, _PERMANENT_CHANNEL_MODE)

    def _handle_disconnect(self) -> None:
        self._health.mark_disconnected(self.connector_id)
        self._is_oper = False  # a reconnect re-runs the OPER handshake in _handle_welcome
        logger.warning("[irc:%s] disconnected", self.connector_id)

    def _handle_join(self, connection, event) -> None:
        # on_join fires for every user joining a channel we're in, not just
        # us - only our own successful join is a health signal.
        if event.source.nick.lower() != connection.get_nickname().lower():
            return
        logger.info("[irc:%s] joined %s", self.connector_id, event.target)
        self._blocked_channels.discard(event.target)
        self._health.record_success(self.connector_id)

    def _handle_join_blocked(self, event) -> None:
        channel = event.arguments[0]
        logger.warning("[irc:%s] join to %s blocked (needs registered/identified nick) - will retry", self.connector_id, channel)
        self._blocked_channels.add(channel)
        self._health.record_error(self.connector_id)

    def _handle_privnotice(self, event) -> None:
        # NickServ's IDENTIFY reply (success or failure - either way, worth
        # a retry now rather than leaving rejected channels unjoined for the
        # rest of the connection). "NickServ" is the same services nick
        # _handle_welcome already sends IDENTIFY to.
        if event.source is None or event.source.nick.lower() != "nickserv":
            return
        logger.debug("[irc:%s] NickServ replied, retrying blocked joins", self.connector_id)
        self._retry_blocked_joins()

    def _retry_blocked_joins(self) -> None:
        if not self._blocked_channels:
            return
        channels = list(self._blocked_channels)
        self._blocked_channels.clear()
        logger.info("[irc:%s] retrying joins for %s", self.connector_id, ", ".join(channels))
        for channel in channels:
            self.connection.join(channel)

    def _handle_privmsg(self, connection, event) -> None:
        # DM to the bot. `STATUS`/`LINKED CHANNELS`/`LINKED USERS` are
        # read-only, no permission gate. `LINK CHANNEL`/`MIRROR CHANNEL`/
        # `UNLINK CHANNEL`/`LINK USER`/`UNLINK USER` (two-token) are oper-gated
        # admin commands, dispatched to _handle_dm_command.
        content = event.arguments[0]
        if content.strip().upper() == "STATUS":
            for line in self._health.render().splitlines():
                connection.notice(event.source.nick, line)
            return
        if content.strip().upper() == "HELP":
            self._notify(event.source.nick, _HELP_TEXT)
            return
        words = content.split()
        if not words:
            return
        two = f"{words[0].upper()} {words[1].upper()}" if len(words) > 1 else ""
        if two == "LINKED CHANNELS":
            self._schedule(self._handle_linked_channels_command(event.source.nick, words[2:]))
            return
        if two == "LINKED USERS":
            self._schedule(self._handle_linked_users_command(event.source.nick, words[2:]))
            return
        if (
            words[0].upper() in _ADMIN_DM_CHANNEL_VERBS
            and len(words) > 1
            and words[1].upper() in _ADMIN_DM_TWO_WORD_NOUNS
        ):
            self._schedule(self._handle_dm_command(event.source.nick, content))

    def _handle_pubnotice(self, event) -> None:
        match = _HISTORY_REPLAY_NOTICE_RE.search(event.arguments[0])
        if match is None:
            return
        channel = event.target.lower()
        logger.debug(
            "[irc:%s] expecting up to %s line(s) of history replay in %s", self.connector_id, match.group(1), channel
        )
        self._history_replay[channel] = (int(match.group(1)), time.monotonic() + _HISTORY_REPLAY_TIMEOUT)

    def _consume_history_replay(self, channel: str) -> bool:
        """True if this message is (probably) server-replayed history, not
        a live message - and, side-effectingly, advances that channel's
        replay budget/expiry so the *next* message gets judged correctly too."""
        key = channel.lower()
        entry = self._history_replay.get(key)
        if entry is None:
            return False
        remaining, deadline = entry
        if time.monotonic() > deadline:
            del self._history_replay[key]
            return False
        remaining -= 1
        if remaining <= 0:
            del self._history_replay[key]
        else:
            self._history_replay[key] = (remaining, deadline)
        return True

    def _handle_pubmsg(self, event) -> None:
        channel = event.target
        if self._consume_history_replay(channel):
            logger.debug("[irc:%s] dropping replayed history line in %s", self.connector_id, channel)
            return  # server-replayed history from joining, not a live message - don't relay it
        content = event.arguments[0]
        logger.debug("[irc:%s] message in %s from %s", self.connector_id, channel, event.source.nick)
        self._schedule(
            self._on_message(
                StandardMessage(
                    origin_connector_id=self.connector_id,
                    origin_channel_id=channel,
                    channel_name=channel,
                    sender_name=event.source.nick,
                    sender_avatar_url=None,
                    sender_user_id=event.source.nick,
                    content_markdown=content,
                    message_id=_synthetic_message_id(channel, event.source.nick, content),
                    attachments=[],
                    source_label=self._config.label,
                )
            )
        )

    async def join_channel(self, channel: str, *, permanent: bool = True, topic: str | None = None) -> None:
        """Called by ChannelLinker right after a fresh mapping involving this
        connector is created, so a newly-linked channel is joined immediately
        instead of waiting for a restart to pick it up from Mongo. `permanent`
        is False for a Discord-thread channel (see ensure_channel), which
        must never get _PERMANENT_CHANNEL_MODE even when `P` is in
        default_channel_modes. `topic`, if given, is set as the channel TOPIC
        - but only when this JOIN just created the channel (issue #32), same
        first-joiner-is-opped reasoning as the MODE line below; on an
        already-existing channel the server bounces it with
        ERR_CHANOPRIVSNEEDED and it's a silent no-op."""
        is_new = channel not in self._channels
        if is_new:
            self._channels.append(channel)
        if self._client.connection.is_connected():
            logger.info("[irc:%s] joining %s", self.connector_id, channel)
            self._client.connection.join(channel)
            if is_new and topic:
                self._client.connection.topic(channel, topic)
            if is_new and self._config.default_channel_modes:
                # Only meaningful if this JOIN just created the channel (the
                # server auto-ops the first joiner of a previously-empty
                # channel) - relies on IRC processing commands from one
                # connection in order, so this MODE lands only after the
                # server has handled the JOIN above. If we're joining a
                # channel that already existed, we won't have ops and the
                # server just bounces this with ERR_CHANOPRIVSNEEDED, which
                # we don't handle - a silent no-op from the bridge's side.
                base_modes, wants_permanent = _split_permanent_mode(self._config.default_channel_modes)
                if base_modes:
                    self._client.connection.mode(channel, base_modes)
                # `P` is oper-only and, for threads, deliberately withheld -
                # so it's split out of the line above and routed through
                # _apply_permanent_mode (which defers it until OPER lands).
                if wants_permanent and permanent:
                    self._apply_permanent_mode(channel)

    def _apply_permanent_mode(self, channel: str) -> None:
        """Set _PERMANENT_CHANNEL_MODE on a freshly-created synced channel,
        or (if the server hasn't confirmed our OPER yet) park it for
        _handle_youreoper to set once it does."""
        if self._is_oper:
            logger.info("[irc:%s] applying %s to synced channel %s", self.connector_id, _PERMANENT_CHANNEL_MODE, channel)
            self._client.connection.mode(channel, _PERMANENT_CHANNEL_MODE)
        else:
            logger.debug("[irc:%s] deferring %s for %s until OPER is confirmed", self.connector_id, _PERMANENT_CHANNEL_MODE, channel)
            self._pending_permanent_modes.add(channel)

    async def part_channel(self, channel: str, unlinked_from: str = "") -> None:
        """Called by ChannelLinker when `channel` has lost its last linked
        counterpart (`/unlink channel`, from any connector) - it's no longer
        bridged, so post a notice saying what it was unlinked from and leave
        it. Idempotent: parting a channel we're not in is a harmless no-op
        on the server."""
        was_tracked = channel in self._channels
        self._channels = [c for c in self._channels if c != channel]
        self._blocked_channels.discard(channel)
        self._pending_permanent_modes.discard(channel)
        if was_tracked and self._client.connection.is_connected():
            logger.info("[irc:%s] parting %s (unlinked from %s)", self.connector_id, channel, unlinked_from or "everything")
            notice = (
                f"This channel was unlinked from {unlinked_from}."
                if unlinked_from
                else "This channel is no longer bridged."
            )
            self._client.connection.privmsg(channel, notice)
            self._client.connection.part(channel, notice)

    async def ensure_channel(
        self,
        name: str,
        category: str | None = None,
        is_thread_category: bool = False,
        category_parent_channel_id: str | None = None,
        *,
        metadata: ChannelMetadata | None = None,
    ) -> str:
        """IRC has no separate channel-creation call - JOINing a channel
        that doesn't exist yet creates it (see join_channel, which already
        handles that + applying default_channel_modes to a freshly-created
        one). Idempotent: joining an already-joined channel is a no-op on
        the server. Channel names get a `#` prefix if missing, since local
        channel names on other connectors (Discord/Stoat) won't have one -
        and are lowercased with runs of whitespace turned into single
        hyphens, since unlike a regular (already-kebab-case) Discord channel
        name, a Discord thread name can contain spaces/capitals, which IRC
        channel names can't. `category` and `category_parent_channel_id` are
        accepted (for signature compatibility with
        ConnectorInfo.ensure_channel) and ignored - IRC has no Category
        concept. `is_thread_category` is honoured only to withhold
        _PERMANENT_CHANNEL_MODE from a thread channel (threads are ephemeral
        - see join_channel's `permanent`). From `metadata` (issue #32) only
        `description` is usable - it becomes the channel TOPIC, set only when
        this JOIN just created the channel (see join_channel); NSFW / icon
        have no IRC equivalent and are ignored."""
        channel = normalize_channel_name(name)
        topic = metadata.description if metadata is not None else None
        await self.join_channel(channel, permanent=not is_thread_category, topic=topic)
        return channel

    async def resolve_channel_id_by_name(self, token: str) -> str | None:
        """Wired into `ConnectorInfo.resolve_channel_id_by_name` so every
        channel command (`/link channel irc general`, `MIRROR CHANNEL …`,
        `/unlink channel …`) accepts a bare channel name and gets the same
        `#name` sterilization `#general` would (issue #41). Unlike
        Discord/Stoat's version this is not a name->id lookup - an IRC
        channel id *is* its name - it just normalizes the token (adds the
        `#`, strips characters IRC channel names can't hold; see
        normalize_channel_name)."""
        return normalize_channel_name(token)

    def normalize_channel_name(self, name: str) -> str:
        """Wired into `ConnectorInfo.normalize_channel_name` so a channel name
        carried over to this connector by `/mirror channel` is stored in the
        same `#name` shape `ensure_channel` gives the id (issue #51) - otherwise
        a channel mirrored as `danksquad` lands with id `#danksquad` but name
        `danksquad`. Synchronous, unlike `resolve_channel_id_by_name`."""
        return normalize_channel_name(name)

    async def list_channels(self) -> list[tuple[str, str]]:
        """Autocomplete source for Discord's `/link channel` `external_id`
        option when its `service` is this IRC connector (issue #41). IRC has
        no queryable channel directory, so this offers the channels this
        connector already knows - the ones from config plus any it's been
        linked into - which is what an operator is picking from anyway. The
        display name drops the `#` (the id keeps it) so the rendered choice
        reads `general (#general)` rather than doubling the prefix."""
        return [(c, c.lstrip("#")) for c in self._channels]

    def _schedule(self, coro: Awaitable[None]) -> None:
        # on_pubmsg/on_privmsg run on the IRC reactor's blocking select loop,
        # which start() below moves onto its own executor thread — hand the
        # coroutine back to the asyncio loop the rest of the bridge runs on.
        if self._loop is None:
            raise RuntimeError("IrcSenderService._schedule() called before start()")
        asyncio.run_coroutine_threadsafe(coro, self._loop)

    async def start(self) -> None:
        # irc.bot.SingleServerIRCBot.start() is a blocking call (its own
        # select loop). Capture the running loop here, on the asyncio thread,
        # before handing the blocking call off to an executor thread so
        # `_schedule` can still reach this loop from callbacks fired there.
        self._loop = asyncio.get_running_loop()
        await self._loop.run_in_executor(None, self._client.start)

    async def close(self) -> None:
        if self._client.connection.is_connected():
            self._client.connection.disconnect("Bridge shutting down")
