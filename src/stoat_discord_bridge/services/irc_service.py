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
import ssl
import time
from collections.abc import Awaitable

import irc.bot
import irc.connection

from stoat_discord_bridge.admin_commands import ChannelLinker, EmoteLinker, LinkError, UserLinker
from stoat_discord_bridge.config import IrcConnectorConfig
from stoat_discord_bridge.models import StandardMessage
from stoat_discord_bridge.services.base import OnMessage, PartialRelayError, ReceiverService, SenderService
from stoat_discord_bridge.services.formatting import chunk_content
from stoat_discord_bridge.services.mentions import rewrite_mentions
from stoat_discord_bridge.status import HealthTracker
from stoat_discord_bridge.storage.user_mappings import UserMappingRepository

# IRC's protocol limit is 512 bytes per raw line, including the
# `:nick!user@host PRIVMSG #channel :` prefix the server sees and the
# trailing CRLF. We don't know our own hostmask as the server will render it,
# so this leaves a generous margin for that plus the "<sender_name> " prefix
# this receiver prepends to each line.
_LINE_LIMIT = 400

# Admin commands (LINK_CHANNEL, LINK_EMOTE, LINK_USER, MIRROR_CHANNEL)
# arrive as a DM to the bot's own nick, bare and uppercase (no leading "/"
# or "!" - unlike Discord/Stoat's slash commands, since many IRC clients
# swallow a leading "/" as a local client command). See
# _handle_privmsg/_handle_dm_command.
_ADMIN_DM_COMMANDS = frozenset({"LINK_CHANNEL", "LINK_EMOTE", "LINK_USER", "MIRROR_CHANNEL"})


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
        emote_linker: EmoteLinker | None = None,
        user_linker: UserLinker | None = None,
    ) -> None:
        SenderService.__init__(self, on_message)
        self._config = config
        self.connector_id = config.id
        self._channels = list(channels)
        self._health = health
        self._linker = linker
        self._emote_linker = emote_linker
        self._user_linker = user_linker
        self._client = _IrcClient(self, config)
        self._loop: asyncio.AbstractEventLoop | None = None
        # Pending WHOIS queries issued by _check_is_oper, keyed by lowercased
        # nick, awaiting resolution from the reactor thread's on_whoisoperator
        # / on_endofwhois / on_nosuchnick callbacks - see _resolve_whois.
        self._pending_whois: dict[str, asyncio.Future] = {}

    @property
    def connection(self):
        return self._client.connection

    def _handle_welcome(self, connection) -> None:
        self._health.mark_connected(self.connector_id)
        if self._config.nickserv_password:
            connection.privmsg("NickServ", f"IDENTIFY {self._config.nickserv_password}")
        if self._config.oper_account and self._config.oper_password:
            # OPER's login name is deliberately not required to match `ident`
            # or `nick` - see IrcConnectorConfig.oper_account.
            connection.oper(self._config.oper_account, self._config.oper_password)
        for channel in self._channels:
            connection.join(channel)

    def _handle_disconnect(self) -> None:
        self._health.mark_disconnected(self.connector_id)

    def _handle_privmsg(self, connection, event) -> None:
        # DM to the bot. `STATUS` reports sync target health (no permission
        # gate - read-only). LINK_CHANNEL/LINK_EMOTE/LINK_USER are
        # oper-gated admin commands, dispatched to _handle_dm_command.
        content = event.arguments[0]
        if content.strip().upper() == "STATUS":
            for line in self._health.render().splitlines():
                connection.notice(event.source.nick, line)
            return
        words = content.split()
        if words and words[0].upper() in _ADMIN_DM_COMMANDS:
            self._schedule(self._handle_dm_command(event.source.nick, content))

    def _handle_pubmsg(self, event) -> None:
        channel = event.target
        content = event.arguments[0]
        self._schedule(
            self._on_message(
                StandardMessage(
                    origin_connector_id=self.connector_id,
                    origin_channel_id=channel,
                    channel_name=channel,
                    sender_name=event.source.nick,
                    sender_avatar_url=None,
                    content_markdown=content,
                    message_id=_synthetic_message_id(channel, event.source.nick, content),
                    attachments=[],
                )
            )
        )

    async def _handle_dm_command(self, nick: str, content: str) -> None:
        command, *args = content.split()
        command = command.upper()
        if not await self._check_is_oper(nick):
            self._notify(nick, "You need to be an IRC operator to do that.")
            return
        if command == "LINK_CHANNEL":
            if len(args) != 3:
                self._notify(nick, "Usage: LINK_CHANNEL <source> <source_id> <local_id>")
                return
            source, source_id, local_id = args
            if self._linker is None:
                self._notify(nick, "Linking isn't configured.")
                return
            try:
                summary = await self._linker.link_channel(
                    local_connector=self.connector_id,
                    local_channel_id=local_id,
                    local_channel_name=local_id,
                    source=source,
                    source_id=source_id,
                    destination_id=local_id,
                )
            except LinkError as exc:
                self._notify(nick, str(exc))
                return
            self._notify(nick, summary)
        elif command == "LINK_EMOTE":
            if len(args) != 3:
                self._notify(nick, "Usage: LINK_EMOTE <source> <source_id> <local_id>")
                return
            source, source_id, local_id = args
            if self._emote_linker is None:
                self._notify(nick, "Linking isn't configured.")
                return
            try:
                summary = await self._emote_linker.link_emote(
                    local_connector=self.connector_id, local_id=local_id, source=source, source_id=source_id
                )
            except LinkError as exc:
                self._notify(nick, str(exc))
                return
            self._notify(nick, summary)
        elif command == "LINK_USER":
            if len(args) != 3:
                self._notify(nick, "Usage: LINK_USER <source> <user_id> <local_user_id>")
                return
            source, user_id, local_user_id = args
            if self._user_linker is None:
                self._notify(nick, "User linking isn't configured.")
                return
            try:
                summary = await self._user_linker.link_user(
                    local_connector=self.connector_id,
                    local_user_id=local_user_id,
                    source=source,
                    source_user_id=user_id,
                )
            except LinkError as exc:
                self._notify(nick, str(exc))
                return
            self._notify(nick, summary)
        elif command == "MIRROR_CHANNEL":
            # Unlike Discord/Stoat, IRC admin commands arrive as a DM with no
            # "current channel" context to default to, so local_channel_id is
            # always required here (not optional like the other two).
            if len(args) != 2:
                self._notify(nick, "Usage: MIRROR_CHANNEL <destination|all> <local_channel_id>")
                return
            destination, local_channel_id = args
            if self._linker is None:
                self._notify(nick, "Linking isn't configured.")
                return
            try:
                if destination.lower() == "all":
                    summary = await self._linker.mirror_channel_all(
                        local_connector=self.connector_id,
                        local_channel_id=local_channel_id,
                        local_channel_name=local_channel_id,
                    )
                else:
                    summary = await self._linker.mirror_channel(
                        local_connector=self.connector_id,
                        local_channel_id=local_channel_id,
                        local_channel_name=local_channel_id,
                        destination=destination,
                    )
            except LinkError as exc:
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

    async def join_channel(self, channel: str) -> None:
        """Called by ChannelLinker right after a fresh mapping involving this
        connector is created, so a newly-linked channel is joined immediately
        instead of waiting for a restart to pick it up from Mongo."""
        is_new = channel not in self._channels
        if is_new:
            self._channels.append(channel)
        if self._client.connection.is_connected():
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
                self._client.connection.mode(channel, self._config.default_channel_modes)

    async def ensure_channel(self, name: str) -> str:
        """IRC has no separate channel-creation call - JOINing a channel
        that doesn't exist yet creates it (see join_channel, which already
        handles that + applying default_channel_modes to a freshly-created
        one). Idempotent: joining an already-joined channel is a no-op on
        the server. Channel names get a `#` prefix if missing, since local
        channel names on other connectors (Discord/Stoat) won't have one."""
        channel = name if name.startswith("#") else f"#{name}"
        await self.join_channel(channel)
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
    def __init__(self, sender: IrcSenderService, user_mappings: UserMappingRepository | None = None) -> None:
        self.connector_id = sender.connector_id
        self._sender = sender
        self._user_mappings = user_mappings

    async def receive(self, message: StandardMessage, *, target_channel_id: str) -> list[str]:
        # TODO: markdown stripping belongs here too.
        content = message.content_markdown
        if self._user_mappings is not None:
            content = await rewrite_mentions(
                content,
                origin_connector_id=message.origin_connector_id,
                target_connector_id=self.connector_id,
                target_kind="irc",
                user_mappings=self._user_mappings,
            )
        prefix = f"<{message.sender_name}> "
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
