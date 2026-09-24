"""`IrcSenderService`: connection lifecycle, channel management, inbound relay.

Instantiated once per configured IRC connector (config.yaml's `irc` list
can have any number of entries). Turns native IRC events into
`StandardMessage`s and owns the shared connection the receiver also posts
through.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import threading
import time
from collections.abc import Awaitable

from stoat_discord_bridge.admin_commands import ChannelLinker, UserLinker, render_help, resolve_help_key
from stoat_discord_bridge.config import IrcConnectorConfig
from stoat_discord_bridge.models import ChannelMetadata, StandardMessage
from stoat_discord_bridge.services.base import OnMessage, SenderService
from stoat_discord_bridge.services.irc_service.client import _IrcClient
from stoat_discord_bridge.services.irc_service.commands import (
    _ADMIN_DM_CHANNEL_VERBS,
    _ADMIN_DM_TWO_WORD_NOUNS,
    IrcAdminCommandsMixin,
)
from stoat_discord_bridge.services.irc_service.formatting import (
    _HISTORY_FETCH_TIMEOUT,
    _HISTORY_REPLAY_NOTICE_RE,
    _HISTORY_REPLAY_TIMEOUT,
    _PERMANENT_CHANNEL_MODE,
    RFC_CHANNEL_NAME_LIMIT,
    HistoryReplayState,
    _split_permanent_mode,
    _synthetic_message_id,
    normalize_channel_name,
    parse_relayed_line,
)
from stoat_discord_bridge.status import HealthTracker

logger = logging.getLogger(__name__)


def _resolve_future(future: "asyncio.Future[list[StandardMessage]]", value: list[StandardMessage]) -> None:
    # Scheduled via call_soon_threadsafe onto the loop thread - by the time
    # this runs, a timed-out fetch_history caller may already have given up
    # and moved on, in which case the future is already done and setting it
    # again would raise InvalidStateError.
    if not future.done():
        future.set_result(value)


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
        # Channel (lowercased) -> HistoryReplayState for a history replay
        # currently in progress - see _handle_pubnotice/_consume_history_replay
        # and _HISTORY_REPLAY_NOTICE_RE's comment.
        self._history_replay: dict[str, HistoryReplayState] = {}
        # Channel (lowercased) -> (capture list, future) for a fetch_history
        # call that's issued its PART+re-JOIN and is waiting for the replay
        # NOTICE that promotes it into _history_replay (see fetch_history/
        # _handle_pubnotice). Kept separate from _history_replay itself so an
        # ordinary join-triggered replay (no pending fetch) can never be
        # mistaken for one still waiting to start, and so a channel with no
        # chanhistory module at all - no NOTICE ever arrives - has something
        # for fetch_history's own timeout to clean up.
        self._pending_history_capture: dict[str, tuple[list[StandardMessage], asyncio.Future]] = {}
        # Guards every read/write of _history_replay and
        # _pending_history_capture: _handle_pubnotice/_consume_history_replay
        # run on the IRC reactor's own OS thread (see start()), while
        # fetch_history runs on the asyncio event-loop thread - a plain
        # threading.Lock, not asyncio.Lock, since one side can't await. Held
        # only across the dict operations themselves, never across an
        # await/network call, so contention is negligible.
        self._history_state_lock = threading.Lock()
        # Per-channel lock serializing fetch_history calls (issue #141): two
        # concurrent calls for the same channel would otherwise clobber each
        # other's _pending_history_capture entry - the second call's PART+
        # re-JOIN overwrites the first's (capture, future) pair, orphaning
        # the first caller's future and then, on its timeout, popping the
        # *second* caller's still-pending entry out from under it. Created
        # lazily per channel key, never removed - cheap, and avoids a
        # dict-mutation race with the lookup itself.
        self._history_fetch_locks: dict[str, asyncio.Lock] = {}
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

    def _channel_name_limit(self) -> int:
        """The channel-name cap to enforce: the server's advertised CHANNELLEN
        ISUPPORT token if we've seen one (some networks set it well below the
        RFC's 50), else `RFC_CHANNEL_NAME_LIMIT`. Backstops `/mirror channel`'s
        own clip to `ConnectorInfo.channel_name_limit` (a static conservative
        default, since the live value isn't known when that's built) - issue #99.
        """
        features = getattr(self.connection, "features", None)
        advertised = getattr(features, "channellen", None)
        if isinstance(advertised, int) and advertised > 0:
            return advertised
        return RFC_CHANNEL_NAME_LIMIT

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
        # DM to the bot. `STATUS`/`HELP`/`LINKED CHANNELS`/`LINKED USERS` are
        # read-only, no permission gate. `LINK CHANNEL`/`MIRROR CHANNEL`/
        # `UNLINK CHANNEL`/`LINK USER`/`UNLINK USER` (two-token) are oper-gated
        # admin commands, dispatched to _handle_dm_command.
        content = event.arguments[0]
        if content.strip().upper() == "STATUS":
            for line in self._health.render().splitlines():
                connection.notice(event.source.nick, line)
            return
        words = content.split()
        if not words:
            return
        if words[0].upper() == "HELP":
            # `HELP [topic] [noun]` - e.g. `HELP MIRROR CHANNEL` (issue #117).
            topic = words[1] if len(words) > 1 else None
            noun = words[2] if len(words) > 2 else None
            self._notify(event.source.nick, render_help(resolve_help_key(topic, noun), connector="irc"))
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
        remaining = int(match.group(1))
        logger.debug(
            "[irc:%s] expecting up to %s line(s) of history replay in %s", self.connector_id, remaining, channel
        )
        # A pending fetch_history call for this channel (if any) hands its
        # capture list/future off to the real HistoryReplayState this NOTICE
        # starts - see fetch_history/_finish_history_capture. Locked: this
        # runs on the reactor thread, fetch_history's dict writes on the
        # loop thread (see _history_state_lock's docstring).
        with self._history_state_lock:
            capture, future = self._pending_history_capture.pop(channel, (None, None))
            if remaining > 0:
                self._history_replay[channel] = HistoryReplayState(
                    remaining=remaining,
                    deadline=time.monotonic() + _HISTORY_REPLAY_TIMEOUT,
                    capture=capture,
                    future=future,
                )
        if remaining <= 0:
            self._finish_history_capture(future, capture)

    def _consume_history_replay(self, channel: str, message: StandardMessage) -> bool:
        """True if `message` is (probably) server-replayed history, not a
        live message - and, side-effectingly, advances that channel's replay
        budget/expiry so the *next* message gets judged correctly too, and
        appends `message` to the entry's capture list if one is in progress
        (a pending fetch_history call, not just an ordinary join-triggered
        replay being dropped). Locked (see _history_state_lock's docstring)
        since fetch_history's timeout path also pops _history_replay, from
        the loop thread rather than this method's reactor thread."""
        key = channel.lower()
        with self._history_state_lock:
            entry = self._history_replay.get(key)
            if entry is None:
                return False
            if time.monotonic() > entry.deadline:
                del self._history_replay[key]
                self._finish_history_capture(entry.future, entry.capture)
                return False
            if entry.capture is not None:
                entry.capture.append(message)
            entry.remaining -= 1
            if entry.remaining <= 0:
                del self._history_replay[key]
                self._finish_history_capture(entry.future, entry.capture)
            return True

    def _finish_history_capture(
        self, future: "asyncio.Future[list[StandardMessage]] | None", capture: list[StandardMessage] | None
    ) -> None:
        """Resolve a pending `fetch_history` call's future with whatever was
        captured, once its replay budget hits zero or its deadline passes.
        A no-op for an ordinary join-triggered replay, which never has a
        future to resolve. `_handle_pubnotice`/`_consume_history_replay` run
        on the IRC reactor's own thread, so the future - an asyncio object -
        can only be touched via call_soon_threadsafe onto the loop thread."""
        if future is None or self._loop is None:
            return
        messages = list(capture) if capture is not None else []
        self._loop.call_soon_threadsafe(_resolve_future, future, messages)

    def _handle_pubmsg(self, event) -> None:
        channel = event.target
        content = event.arguments[0]
        message = StandardMessage(
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
        if self._consume_history_replay(channel, message):
            logger.debug("[irc:%s] capturing/dropping replayed history line in %s", self.connector_id, channel)
            return  # server-replayed history from joining, not a live message - don't relay it
        logger.debug("[irc:%s] message in %s from %s", self.connector_id, channel, event.source.nick)
        self._schedule(self._on_message(message))

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

    def chanhistory_configured(self) -> bool:
        """Whether this connector's `default_channel_modes` enables the
        chanhistory-replay module (`H`) - the signal `ConnectorInfo.
        supports_history_destination` wires for the IRC -> IRC `with_history`
        decision (issue #141): a config-level check (this connector's own
        setting, not a live per-channel MODE query) so a backfill *into* an
        IRC channel is refused up front when nothing would actually persist
        it, matching this feature's existing "gold setup" scoping for
        chanhistory detection elsewhere in this module. A plain method, not a
        property - `supports_history_destination` is wired to it directly as
        a bound callable (`ConnectorInfo`'s hook contract), not called."""
        return "H" in (self._config.default_channel_modes or "")

    async def fetch_history(
        self, channel_id: str, limit: int | None, *, include_relayed: bool = False
    ) -> list[StandardMessage]:
        """`ConnectorInfo.fetch_history` for IRC (issue #141): unlike
        Discord/Stoat, IRC has no on-demand history query, no CAP
        negotiation on this network at all, and no per-channel scrollback API
        - chanhistory only ever replays as a side effect of JOIN. Getting a
        *fresh* snapshot on demand therefore means forcing a PART and
        immediate re-JOIN, then capturing the replay burst that follows
        (`_consume_history_replay`, diverted into capture mode via
        `_pending_history_capture`/`HistoryReplayState.capture` instead of
        its ordinary drop-only behavior) rather than relaying it live.

        Resolves to an empty list if no matching NOTICE arrives at all
        within `_HISTORY_FETCH_TIMEOUT` (module not installed/enabled on
        this channel or network) - the same "nothing to backfill" outcome
        every other connector's `fetch_history` produces in the no-history
        case; no separate capability flag needed for the *source* side (see
        `chanhistory_configured` for the *destination*-side gate).

        `limit` can only narrow what the server's own chanhistory cap
        already chose to replay - there's no way to ask for more than its
        configured maximum - so it's applied by slicing the captured list
        client-side to the most recent `limit` messages, the same caveat
        Stoat's 100-per-page pagination cap already documents.

        Known side effect (documented in CLAUDE.md/COMMANDS.md): the channel
        is genuinely PARTed and re-JOINed while this is in flight (typically
        well under `_HISTORY_REPLAY_TIMEOUT`), during which anything sent
        there won't relay live.

        Two concurrent calls for the same channel are serialized (via
        `_history_fetch_locks`) rather than run in parallel - a second
        PART+re-JOIN before the first's capture finished would clobber its
        `_pending_history_capture` entry and orphan its future.

        `include_relayed` (issue #161's `/import` / `/export`) unpacks the
        bridge's own relayed lines back into their original sender
        (`_unpack_relayed_line`); the capture itself already holds every
        nick's lines."""
        channel = normalize_channel_name(channel_id, self._channel_name_limit())
        key = channel.lower()
        if not self._client.connection.is_connected():
            return []
        lock = self._history_fetch_locks.setdefault(key, asyncio.Lock())
        async with lock:
            loop = asyncio.get_running_loop()
            future: "asyncio.Future[list[StandardMessage]]" = loop.create_future()
            capture: list[StandardMessage] = []
            with self._history_state_lock:
                self._pending_history_capture[key] = (capture, future)
            logger.info("[irc:%s] refreshing history for %s (part/rejoin)", self.connector_id, channel)
            self._client.connection.part(channel, "refreshing history")
            self._client.connection.join(channel)
            try:
                messages = await asyncio.wait_for(future, timeout=_HISTORY_FETCH_TIMEOUT)
            except asyncio.TimeoutError:
                with self._history_state_lock:
                    self._pending_history_capture.pop(key, None)
                    self._history_replay.pop(key, None)
                messages = capture
        if limit is not None:
            messages = messages[-limit:] if limit > 0 else []
        if include_relayed:
            messages = [self._unpack_relayed_line(m) for m in messages]
        return messages

    def _unpack_relayed_line(self, message: StandardMessage) -> StandardMessage:
        """A captured line from the bridge's own nick, read back as the sender
        it was relayed for (issue #161): `<alice, Discord> hi` becomes sender
        `alice [Discord]` with content `hi`, and no source label so the
        destination doesn't decorate it again. Anything else is returned
        unchanged, including an own line with no tag."""
        if message.sender_user_id.lower() != self._client.connection.get_nickname().lower():
            return message
        parsed = parse_relayed_line(message.content_markdown)
        if parsed is None:
            return message
        sender, content = parsed
        return dataclasses.replace(message, sender_name=sender, content_markdown=content, source_label=None)

    async def ensure_channel(
        self,
        name: str,
        category: str | None = None,
        is_thread_category: bool = False,
        category_parent_channel_id: str | None = None,
        *,
        metadata: ChannelMetadata | None = None,
        is_voice: bool = False,
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
        concept. `is_thread_category` is honored only to withhold
        _PERMANENT_CHANNEL_MODE from a thread channel (threads are ephemeral
        - see join_channel's `permanent`). From `metadata` (issue #32) only
        `description` is usable - it becomes the channel TOPIC, set only when
        this JOIN just created the channel (see join_channel); NSFW / icon /
        slowmode_delay (issue #108) have no IRC equivalent and are ignored.
        `is_voice` (issue #146) is likewise accepted and ignored - IRC has no
        voice-channel concept, so mirroring a Discord/Stoat voice channel to
        IRC still lands as an ordinary flat channel rather than raising for
        an unexpected keyword. The `#name` is truncated to the
        server's CHANNELLEN (or the RFC default) as a backstop, since a name
        can reach here from paths other than `/mirror` - issue #99."""
        channel = normalize_channel_name(name, self._channel_name_limit())
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
        normalize_channel_name), truncating to CHANNELLEN so it matches the id
        `ensure_channel` produces (issue #99)."""
        return normalize_channel_name(token, self._channel_name_limit())

    def normalize_channel_name(self, name: str) -> str:
        """Wired into `ConnectorInfo.normalize_channel_name` so a channel name
        carried over to this connector by `/mirror channel` is stored in the
        same `#name` shape `ensure_channel` gives the id (issue #51) - otherwise
        a channel mirrored as `danksquad` lands with id `#danksquad` but name
        `danksquad`. Truncates to CHANNELLEN for the same reason (issue #99).
        Synchronous, unlike `resolve_channel_id_by_name`."""
        return normalize_channel_name(name, self._channel_name_limit())

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
