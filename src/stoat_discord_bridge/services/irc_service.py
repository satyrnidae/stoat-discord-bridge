"""IRC sender/receiver services.

Instantiated once per configured IRC connector (config.yaml's `irc` list
can have any number of entries).

IRC has no per-message identity override and no native attachment/markdown
support, so the receiver posts through the same connection as the sender,
prefixing each line with the remote user's name — see the TODOs in
`IrcReceiverService.receive()` for the rest of the formatting work.
"""

from __future__ import annotations

import asyncio
import functools
import hashlib
import logging
import re
import ssl
import time
from collections.abc import Awaitable

import irc.bot
import irc.connection

from stoat_discord_bridge.admin_commands import ChannelLinker, LinkError, UserLinker
from stoat_discord_bridge.config import IrcConnectorConfig
from stoat_discord_bridge.models import StandardMessage
from stoat_discord_bridge.services.base import OnMessage, PartialRelayError, ReceiverService, SenderService
from stoat_discord_bridge.services.formatting import chunk_content, render_discord_timestamps
from stoat_discord_bridge.services.mentions import (
    rewrite_channel_mentions,
    rewrite_mentions,
    rewrite_role_mentions,
)
from stoat_discord_bridge.status import HealthTracker
from stoat_discord_bridge.storage.channel_mappings import ChannelMappingRepository
from stoat_discord_bridge.storage.role_mappings import RoleMappingRepository
from stoat_discord_bridge.storage.user_mappings import UserMappingRepository

logger = logging.getLogger(__name__)

# IRC's protocol limit is 512 bytes per raw line, including the
# `:nick!user@host PRIVMSG #channel :` prefix the server sees and the
# trailing CRLF. We don't know our own hostmask as the server will render it,
# so this leaves a generous margin for that plus the "<sender_name> " prefix
# this receiver prepends to each line.
_LINE_LIMIT = 400

# InspIRCd's +P keeps a channel (and its modes/topic/bans) alive on the
# server even while it's empty, so a synced channel mirrored here doesn't
# evaporate between messages. It's an oper-only mode, so it can only be set
# once our OPER handshake (_handle_welcome) has been confirmed by the server
# (RPL_YOUREOPER -> on_youreoper); a synced channel created before then is
# parked in _pending_permanent_modes and gets +P the moment we're opered.
# Applied only when `P` is one of the connector's configured
# default_channel_modes, and never to Discord-thread channels
# (ensure_channel's is_thread_category) - threads are ephemeral, so their
# mirror channel should be free to disappear when empty like any other.
_PERMANENT_CHANNEL_MODE = "+P"


def _split_permanent_mode(modes: str) -> tuple[str | None, bool]:
    """Split a MODE string like `+HtnPR` into (`+HtnR`, True) - the modes to
    apply to a channel immediately on creation, and whether `P` (handled
    separately, oper-gated) was among them. Returns (None, ...) when nothing
    but a bare sign is left."""
    had_p = "P" in modes
    if not had_p:
        return modes, False
    stripped = modes.replace("P", "")
    return (None if stripped.strip(" +-") == "" else stripped), True

# Admin commands arrive as a DM to the bot's own nick, bare and uppercase
# (no leading "/" or "!" - unlike Discord/Stoat's slash commands, since many
# IRC clients swallow a leading "/" as a local client command). The channel
# and user commands are two-token (`LINK CHANNEL` / `MIRROR CHANNEL` /
# `UNLINK CHANNEL` / `LINK USER` / `UNLINK USER`, and read-only `LINKED
# CHANNELS` / `LINKED USERS`), matching Discord's `/link channel` subcommand
# shape. IRC has no custom-emoji concept, so the emote commands aren't
# offered here at all (same as roles/categories). See
# _handle_privmsg/_handle_dm_command.
_ADMIN_DM_CHANNEL_VERBS = frozenset({"LINK", "MIRROR", "UNLINK"})
# Second tokens accepted after a verb in _ADMIN_DM_CHANNEL_VERBS.
_ADMIN_DM_TWO_WORD_NOUNS = frozenset({"CHANNEL", "USER"})

# IRC has no slash-command discoverability at all, hence HELP. See
# COMMANDS.md for full per-command detail - this is a compact pointer to it.
_HELP_TEXT = """Commands (DM me, bare and uppercase - see COMMANDS.md for full detail):
  STATUS - sync target health, read-only
  LINKED CHANNELS <local_id> - channels bridged to <local_id>, read-only
  LINKED USERS [local_id|name] - cross-connector user links, read-only
  LINK CHANNEL <local_id> <service> <external_id> - bridge a channel (IRC-operator)
  LINK USER <service> <external_id|name> <local_id|name> - link a user for mentions/masquerading (IRC-operator)
  MIRROR CHANNEL <local_id> [service|all] - create+link a matching channel (IRC-operator)
  UNLINK CHANNEL <local_id> [service|all] - unlink a channel from one connector, or the whole group (IRC-operator)
  UNLINK USER [service|all] [local_id|name] - unlink a user (default: yourself) from one connector, or the whole group (IRC-operator)
  HELP - this message"""

# irc.satyrn.dev (InspIRCd-4 + a chanhistory-style module, enabled via the
# `H` in default_channel_modes) replays recent history to a channel right
# after JOIN, announced by a channel NOTICE of this exact shape (confirmed
# by a live probe against the real server - not a guess):
#   "Replaying up to 50 lines of pre-join history from the last ..."
# immediately followed by that many PRIVMSGs, indistinguishable from live
# traffic otherwise. This network doesn't support CAP negotiation at all
# (CAP LS gets 421 Unknown command), so there's no IRCv3 batch/server-time
# to lean on, and there's no explicit "end of replay" marker either - only
# the server's own "up to N" cap on how many lines *could* follow.
_HISTORY_REPLAY_NOTICE_RE = re.compile(r"Replaying up to (\d+) lines? of pre-join history", re.IGNORECASE)
# Safety net for a channel whose actual history was shorter than the
# announced cap: without this, the leftover "budget" would silently
# swallow the next several genuinely-live messages whenever they next
# arrive, however much later. Observed replay bursts land in a single TCP
# read (sub-second), so this is generous, not tight.
_HISTORY_REPLAY_TIMEOUT = 5.0


def _tls_wrap(sock, *, server_hostname: str, client_cert_file: str | None, client_key_file: str | None):
    # ssl.SSLContext.wrap_socket() defaults to check_hostname=True, which
    # raises ValueError unless server_hostname is supplied.
    context = ssl.create_default_context()
    if client_cert_file:
        # client_key_file may be None if client_cert_file is a combined
        # cert+key PEM - load_cert_chain accepts that.
        context.load_cert_chain(certfile=client_cert_file, keyfile=client_key_file)
    return context.wrap_socket(sock, server_hostname=server_hostname)


class _IrcClient(irc.bot.SingleServerIRCBot):
    """The irc library dispatches events by looking up `on_<event>` methods on
    the bot instance itself, so *something* has to subclass SingleServerIRCBot.
    This subclass exists only to satisfy that and delegates every callback to
    the owning IrcSenderService, which otherwise doesn't need to inherit from
    a third-party client class."""

    def __init__(self, owner: IrcSenderService, config: IrcConnectorConfig) -> None:
        connect_factory = (
            irc.connection.Factory(
                wrapper=functools.partial(
                    _tls_wrap,
                    server_hostname=config.host,
                    client_cert_file=config.client_cert_file,
                    client_key_file=config.client_key_file,
                )
            )
            if config.use_tls
            else irc.connection.Factory()
        )
        # `username` (the ident/USER-command field) is forwarded straight
        # through to ServerConnection.connect() the same way connect_factory
        # is, per SingleServerIRCBot's **connect_params passthrough - falls
        # back to its own default (the nickname) if we don't set it.
        # TODO: unverified against the installed `irc` library version, same
        # caveat as the rest of this module's irc.bot usage.
        username_kwarg = {"username": config.ident} if config.ident else {}
        super().__init__(
            server_list=[(config.host, config.port)],
            nickname=config.nick,
            realname=config.nick,
            connect_factory=connect_factory,
            **username_kwarg,
        )
        self._owner = owner

    def on_welcome(self, connection, event) -> None:
        self._owner._handle_welcome(connection)

    def on_disconnect(self, connection, event) -> None:
        self._owner._handle_disconnect()

    def on_privmsg(self, connection, event) -> None:
        self._owner._handle_privmsg(connection, event)

    def on_pubmsg(self, connection, event) -> None:
        self._owner._handle_pubmsg(event)

    def on_pubnotice(self, connection, event) -> None:
        self._owner._handle_pubnotice(event)

    def on_privnotice(self, connection, event) -> None:
        self._owner._handle_privnotice(event)

    def on_join(self, connection, event) -> None:
        self._owner._handle_join(connection, event)

    def on_nochanmodes(self, connection, event) -> None:
        # Numeric 477. RFC2812 defines this as ERR_NOCHANMODES ("channel
        # doesn't support modes"), but InspIRCd's services module (this
        # bridge's target network - see the history-replay heuristic above)
        # repurposes it as ERR_NEEDREGGEDNICK: "you need to be identified to
        # a registered account to join this channel" - the failure mode this
        # handler exists for. A network that sends 477 for its RFC meaning
        # instead would just cause a harmless spurious rejoin attempt here.
        self._owner._handle_join_blocked(event)

    def on_youreoper(self, connection, event) -> None:
        # Numeric 381 (RPL_YOUREOPER) - the server confirming the OPER
        # command _handle_welcome sent succeeded.
        self._owner._handle_youreoper()

    def on_whoisoperator(self, connection, event) -> None:
        # Fired only if the WHOIS target IS an oper - always arrives before
        # on_endofwhois for the same WHOIS exchange.
        self._owner._resolve_whois(event.arguments[0], True)

    def on_endofwhois(self, connection, event) -> None:
        # Always fires at the end of a WHOIS exchange, oper or not. If
        # on_whoisoperator already resolved this nick's future to True,
        # _resolve_whois is a no-op here - see its docstring.
        self._owner._resolve_whois(event.arguments[0], False)

    def on_nosuchnick(self, connection, event) -> None:
        # WHOIS target doesn't exist (e.g. they disconnected between sending
        # the DM and this WHOIS resolving) - treat as "not oper".
        self._owner._resolve_whois(event.arguments[0], False)


class IrcSenderService(SenderService):
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
                )
            )
        )

    async def _handle_linked_channels_command(self, nick: str, args: list[str]) -> None:
        # Unlike Discord/Stoat, an IRC DM has no "current channel" context to
        # default to (same reasoning as MIRROR CHANNEL's local_id),
        # so the channel to look up is always required here.
        if len(args) != 1:
            self._notify(nick, "Usage: LINKED CHANNELS <local_id>")
            return
        if self._linker is None:
            self._notify(nick, "Linking isn't configured.")
            return
        summary = await self._linker.list_linked_channels(local_connector=self.connector_id, local_channel_id=args[0])
        self._notify(nick, summary)

    async def _handle_linked_users_command(self, nick: str, args: list[str]) -> None:
        """`LINKED USERS [local_id]`: with no argument, lists every
        cross-connector user link (for debugging); given an IRC nick, shows
        just that identity's link."""
        if self._user_linker is None:
            self._notify(nick, "Linking isn't configured.")
            return
        if args:
            summary = await self._user_linker.list_linked_users(local_connector=self.connector_id, local_user_id=args[0])
        else:
            summary = await self._user_linker.list_linked_users()
        self._notify(nick, summary)

    async def _handle_dm_command(self, nick: str, content: str) -> None:
        parts = content.split()
        if len(parts) > 1 and parts[0].upper() in _ADMIN_DM_CHANNEL_VERBS and parts[1].upper() in _ADMIN_DM_TWO_WORD_NOUNS:
            noun = parts[1].upper()
            # Channel commands keep the two-word `LINK CHANNEL` form the
            # branch bodies match on; user commands fold to `LINK_USER` /
            # `UNLINK_USER` to match theirs.
            sep = " " if noun == "CHANNEL" else "_"
            command, args = f"{parts[0].upper()}{sep}{noun}", parts[2:]
        else:
            command, *args = parts
            command = command.upper()
        if not await self._check_is_oper(nick):
            logger.info("[irc:%s] %s tried admin command %s without oper status - denied", self.connector_id, nick, command)
            self._notify(nick, "You need to be an IRC operator to do that.")
            return
        logger.info("[irc:%s] %s ran %s %s", self.connector_id, nick, command, " ".join(args))
        if command == "LINK CHANNEL":
            if len(args) != 3:
                self._notify(nick, "Usage: LINK CHANNEL <local_id> <service> <external_id>")
                return
            local_id, service, external_id = args
            if self._linker is None:
                self._notify(nick, "Linking isn't configured.")
                return
            try:
                summary = await self._linker.link_channel(
                    local_connector=self.connector_id,
                    local_channel_id=local_id,
                    local_channel_name=local_id,
                    source=service,
                    source_id=external_id,
                    destination_id=local_id,
                )
            except LinkError as exc:
                logger.info("[irc:%s] %s rejected: %s", self.connector_id, command, exc)
                self._notify(nick, str(exc))
                return
            self._notify(nick, summary)
        elif command == "LINK_USER":
            if len(args) != 3:
                self._notify(nick, "Usage: LINK USER <service> <external_id|name> <local_id|name>")
                return
            service, external_id, local_id = args
            if self._user_linker is None:
                self._notify(nick, "User linking isn't configured.")
                return
            try:
                summary = await self._user_linker.link_user(
                    local_connector=self.connector_id,
                    local_user_id=local_id,
                    source=service,
                    source_user_id=external_id,
                )
            except LinkError as exc:
                logger.info("[irc:%s] %s rejected: %s", self.connector_id, command, exc)
                self._notify(nick, str(exc))
                return
            self._notify(nick, summary)
        elif command == "MIRROR CHANNEL":
            # Unlike Discord/Stoat, IRC admin commands arrive as a DM with no
            # "current channel" context to default to, so local_id is
            # always required here - hoisted to the first arg, same
            # convention as LINK CHANNEL / UNLINK CHANNEL. service is
            # optional and defaults to "all".
            if len(args) == 1:
                local_id, service = args[0], None
            elif len(args) == 2:
                local_id, service = args
            else:
                self._notify(nick, "Usage: MIRROR CHANNEL <local_id> [service|all]")
                return
            if self._linker is None:
                self._notify(nick, "Linking isn't configured.")
                return
            try:
                if service is None or service.lower() == "all":
                    summary = await self._linker.mirror_channel_all(
                        local_connector=self.connector_id,
                        local_channel_id=local_id,
                        local_channel_name=local_id,
                    )
                else:
                    summary = await self._linker.mirror_channel(
                        local_connector=self.connector_id,
                        local_channel_id=local_id,
                        local_channel_name=local_id,
                        destination=service,
                    )
            except LinkError as exc:
                logger.info("[irc:%s] %s rejected: %s", self.connector_id, command, exc)
                self._notify(nick, str(exc))
                return
            self._notify(nick, summary)
        elif command == "UNLINK CHANNEL":
            # Same "no current channel" reasoning as MIRROR CHANNEL -
            # local_id is always required, hoisted to the first arg.
            # service is optional and defaults to "all" (dissolving the
            # whole bridge group), so 1 arg is just the channel and 2 args
            # are channel then service.
            if len(args) == 1:
                local_id, service = args[0], None
            elif len(args) == 2:
                local_id, service = args
            else:
                self._notify(nick, "Usage: UNLINK CHANNEL <local_id> [service|all]")
                return
            if self._linker is None:
                self._notify(nick, "Linking isn't configured.")
                return
            try:
                summary = await self._linker.unlink_channel(
                    local_connector=self.connector_id, local_channel_id=local_id, destination=service
                )
            except LinkError as exc:
                logger.info("[irc:%s] %s rejected: %s", self.connector_id, command, exc)
                self._notify(nick, str(exc))
                return
            self._notify(nick, summary)
        elif command == "UNLINK_USER":
            # Unlike UNLINK CHANNEL, both args are optional here: service
            # defaults to "all", and local_id defaults to the nick
            # running the command - IRC has no "current channel" to fall
            # back to, but it does always know who's asking.
            if len(args) > 2:
                self._notify(nick, "Usage: UNLINK USER [service|all] [local_id|name]")
                return
            service = args[0] if args else None
            local_id = args[1] if len(args) > 1 else nick
            if self._user_linker is None:
                self._notify(nick, "User linking isn't configured.")
                return
            try:
                summary = await self._user_linker.unlink_user(
                    local_connector=self.connector_id, local_user_id=local_id, destination=service
                )
            except LinkError as exc:
                logger.info("[irc:%s] %s rejected: %s", self.connector_id, command, exc)
                self._notify(nick, str(exc))
                return
            self._notify(nick, summary)

    async def _check_is_oper(self, nick: str) -> bool:
        # WHOIS is async on this library (reply numerics arrive later, on
        # the reactor thread) - issue the query and await its resolution via
        # a Future that _resolve_whois fulfils from that thread. Always live
        # (never cached), since oper status can be granted/revoked at any
        # time on the network.
        key = nick.lower()
        future: asyncio.Future = self._loop.create_future()
        self._pending_whois[key] = future
        self.connection.whois([nick])
        try:
            return await asyncio.wait_for(future, timeout=10)
        except asyncio.TimeoutError:
            logger.warning("[irc:%s] WHOIS for %s timed out - treating as not-oper", self.connector_id, nick)
            return False
        finally:
            self._pending_whois.pop(key, None)

    def _resolve_whois(self, nick: str, is_oper: bool) -> None:
        # Runs on the IRC reactor's own thread (see _IrcClient's on_whois*
        # callbacks) - the Future it resolves is awaited by a coroutine on
        # the asyncio loop, so it must be resolved via call_soon_threadsafe,
        # never by calling set_result directly from this thread.
        future = self._pending_whois.get(nick.lower())
        if future is None or future.done():
            # Either no _check_is_oper call is waiting on this nick, or
            # on_whoisoperator already resolved it True and this is the
            # trailing on_endofwhois/on_nosuchnick for the same exchange.
            return
        self._loop.call_soon_threadsafe(future.set_result, is_oper)

    def _notify(self, nick: str, text: str) -> None:
        for line in text.splitlines():
            self.connection.notice(nick, line)

    async def join_channel(self, channel: str, *, permanent: bool = True) -> None:
        """Called by ChannelLinker right after a fresh mapping involving this
        connector is created, so a newly-linked channel is joined immediately
        instead of waiting for a restart to pick it up from Mongo. `permanent`
        is False for a Discord-thread channel (see ensure_channel), which
        must never get _PERMANENT_CHANNEL_MODE even when `P` is in
        default_channel_modes."""
        is_new = channel not in self._channels
        if is_new:
            self._channels.append(channel)
        if self._client.connection.is_connected():
            logger.info("[irc:%s] joining %s", self.connector_id, channel)
            self._client.connection.join(channel)
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
        - see join_channel's `permanent`)."""
        normalized = re.sub(r"\s+", "-", name.strip().lower())
        channel = normalized if normalized.startswith("#") else f"#{normalized}"
        await self.join_channel(channel, permanent=not is_thread_category)
        return channel

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


def _synthetic_message_id(channel: str, nick: str, content: str) -> str:
    # IRC has no native message IDs. Hash the message contents (scoped by
    # channel/nick, salted with receipt time to keep repeated identical
    # messages from colliding) so sync tracking has a stable per-message key.
    digest = hashlib.sha256(f"{channel}\0{nick}\0{content}\0{time.time_ns()}".encode()).hexdigest()
    return f"irc-{digest[:16]}"


class IrcReceiverService(ReceiverService):
    def __init__(
        self,
        sender: IrcSenderService,
        user_mappings: UserMappingRepository | None = None,
        enable_local_user_masquerade: bool = True,
        channel_mappings: ChannelMappingRepository | None = None,
        role_mappings: RoleMappingRepository | None = None,
    ) -> None:
        self.connector_id = sender.connector_id
        self._sender = sender
        self._user_mappings = user_mappings
        self._enable_local_user_masquerade = enable_local_user_masquerade
        self._channel_mappings = channel_mappings
        self._role_mappings = role_mappings

    async def receive(self, message: StandardMessage, *, target_channel_id: str) -> list[str]:
        # TODO: markdown stripping belongs here too.
        content = message.content_markdown
        # IRC has no native attachments - inline each attachment URL (a
        # Discord/Stoat CDN link) as its own line so an image-only message
        # isn't relayed blank. Kept inline rather than using
        # content_with_attachments(), whose empty-message sentinel would put
        # a zero-width space on the wire.
        if message.attachments:
            extra = "\n".join(a.url for a in message.attachments if a.url)
            if extra:
                content = f"{content}\n{extra}" if content else extra
        # Discord/Stoat <t:...> dynamic timestamps have no IRC equivalent - render
        # them to plain text (relative styles are relative to right now, i.e. when
        # this handler runs).
        content = render_discord_timestamps(content)
        sender_name = message.sender_name
        if self._user_mappings is not None:
            content = await rewrite_mentions(
                content,
                origin_connector_id=message.origin_connector_id,
                target_connector_id=self.connector_id,
                target_kind="irc",
                user_mappings=self._user_mappings,
            )
            if self._enable_local_user_masquerade:
                # A linked sender's user_id on IRC IS the nick (see
                # storage/user_mappings.py's UserMapping.user_id docstring), so
                # unlike Discord/Stoat this needs no further identity lookup.
                local_nick = await self._user_mappings.find_linked_user_id(
                    message.origin_connector_id, message.sender_user_id, self.connector_id
                )
                if local_nick is not None:
                    logger.debug(
                        "[irc:%s] resolved local user masquerade identity for %s: nick=%r",
                        self.connector_id,
                        message.sender_user_id,
                        local_nick,
                    )
                    sender_name = local_nick
            else:
                logger.debug(
                    "[irc:%s] local user masquerade disabled (enable_local_user_masquerade=false), "
                    "not resolving local nick for sender %s",
                    self.connector_id,
                    message.sender_user_id,
                )
        if self._channel_mappings is not None:
            content = await rewrite_channel_mentions(
                content,
                origin_connector_id=message.origin_connector_id,
                target_connector_id=self.connector_id,
                target_kind="irc",
                channel_mappings=self._channel_mappings,
            )
        if self._role_mappings is not None:
            content = await rewrite_role_mentions(
                content,
                origin_connector_id=message.origin_connector_id,
                target_connector_id=self.connector_id,
                target_kind="irc",
                role_mappings=self._role_mappings,
            )
        if not content.strip():
            # A synced message with no textual content (after attachment
            # inlining and mention/timestamp rewrites) has nothing to show on
            # IRC. This is how IRC ignores pin/unpin notifications, which
            # Discord/Stoat relay as content-less messages - IRC has no
            # message-pin concept, so it just drops them.
            logger.debug(
                "[irc:%s] dropping content-less synced message into %s", self.connector_id, target_channel_id
            )
            return []
        prefix = f"<{sender_name}> "
        limit = max(1, _LINE_LIMIT - len(prefix))
        ids: list[str] = []
        for line in content.splitlines() or [""]:
            for chunk in chunk_content(line, limit):
                try:
                    self._sender.connection.privmsg(target_channel_id, f"{prefix}{chunk}")
                except Exception as exc:
                    raise PartialRelayError(ids, exc) from exc
                # IRC has no native message ID to echo back; synthesize one
                # (same scheme as inbound messages) so each post still gets a
                # distinct, non-colliding sync-tracking key.
                ids.append(_synthetic_message_id(target_channel_id, message.sender_name, chunk))
        return ids
