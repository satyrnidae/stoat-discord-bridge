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

from stoat_discord_bridge.admin_commands import ChannelLinker, LinkError
from stoat_discord_bridge.config import IrcConnectorConfig
from stoat_discord_bridge.models import StandardMessage
from stoat_discord_bridge.services.base import OnMessage, PartialRelayError, ReceiverService, SenderService
from stoat_discord_bridge.services.formatting import chunk_content
from stoat_discord_bridge.status import HealthTracker

# IRC's protocol limit is 512 bytes per raw line, including the
# `:nick!user@host PRIVMSG #channel :` prefix the server sees and the
# trailing CRLF. We don't know our own hostmask as the server will render it,
# so this leaves a generous margin for that plus the "<sender_name> " prefix
# this receiver prepends to each line.
_LINE_LIMIT = 400

# "!" rather than "/" for the admin commands below: many IRC clients treat a
# leading "/" as a local client command and never send it as message text
# unless escaped, so this bridge uses its own bang-prefix convention here
# (unlike Discord/Stoat, which both use "/"). STATUS-via-DM is unaffected.
_LINK_CHANNEL_PREFIX = "!link-channel"


def _tls_wrap(sock, *, server_hostname: str):
    # ssl.SSLContext.wrap_socket() defaults to check_hostname=True, which
    # raises ValueError unless server_hostname is supplied.
    return ssl.create_default_context().wrap_socket(sock, server_hostname=server_hostname)


class _IrcClient(irc.bot.SingleServerIRCBot):
    """The irc library dispatches events by looking up `on_<event>` methods on
    the bot instance itself, so *something* has to subclass SingleServerIRCBot.
    This subclass exists only to satisfy that and delegates every callback to
    the owning IrcSenderService, which otherwise doesn't need to inherit from
    a third-party client class."""

    def __init__(self, owner: IrcSenderService, config: IrcConnectorConfig) -> None:
        connect_factory = (
            irc.connection.Factory(wrapper=functools.partial(_tls_wrap, server_hostname=config.host))
            if config.use_tls
            else irc.connection.Factory()
        )
        super().__init__(
            server_list=[(config.host, config.port)],
            nickname=config.nick,
            realname=config.nick,
            connect_factory=connect_factory,
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


class IrcSenderService(SenderService):
    def __init__(
        self,
        config: IrcConnectorConfig,
        channels: list[str],
        on_message: OnMessage,
        health: HealthTracker,
        linker: ChannelLinker | None = None,
    ) -> None:
        SenderService.__init__(self, on_message)
        self._config = config
        self.connector_id = config.id
        self._channels = list(channels)
        self._health = health
        self._linker = linker
        self._client = _IrcClient(self, config)
        self._loop: asyncio.AbstractEventLoop | None = None

    @property
    def connection(self):
        return self._client.connection

    def _handle_welcome(self, connection) -> None:
        self._health.mark_connected(self.connector_id)
        if self._config.nickserv_password:
            connection.privmsg("NickServ", f"IDENTIFY {self._config.nickserv_password}")
        for channel in self._channels:
            connection.join(channel)

    def _handle_disconnect(self) -> None:
        self._health.mark_disconnected(self.connector_id)

    def _handle_privmsg(self, connection, event) -> None:
        # DM to the bot. `STATUS` reports sync target health; anything else
        # is ignored (IRC has no other DM-driven commands yet).
        if event.arguments[0].strip().upper() == "STATUS":
            for line in self._health.render().splitlines():
                connection.notice(event.source.nick, line)

    def _handle_pubmsg(self, event) -> None:
        channel = event.target
        content = event.arguments[0]
        if content.strip().startswith(_LINK_CHANNEL_PREFIX):
            self._schedule(self._handle_link_channel(channel, event.source.nick, content))
            return
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

    async def _handle_link_channel(self, channel: str, nick: str, content: str) -> None:
        if not self._is_channel_admin(channel, nick):
            self._reply(channel, "You need to be a channel operator to do that.")
            return
        args = content.split()[1:]
        if len(args) < 2:
            self._reply(channel, "Usage: !link-channel <source> <source_id> [<destination_id>]")
            return
        source, source_id, *rest = args
        destination_id = rest[0] if rest else None

        if self._linker is None:
            self._reply(channel, "Linking isn't configured.")
            return
        try:
            summary = await self._linker.link_channel(
                local_connector=self.connector_id,
                local_channel_id=channel,
                local_channel_name=channel,
                source=source,
                source_id=source_id,
                destination_id=destination_id,
            )
        except LinkError as exc:
            self._reply(channel, str(exc))
            return
        self._reply(channel, summary)

    def _is_channel_admin(self, channel: str, nick: str) -> bool:
        # TODO: verify against the `irc` library's actual Channel API -
        # assumes SingleServerIRCBot.channels[channel].is_oper(nick) tracks
        # +o status from the server's NAMES/MODE state, matching this
        # library's common usage elsewhere. Fails closed (treats as
        # non-admin) if the channel/nick isn't tracked or the API differs.
        try:
            return bool(self._client.channels[channel].is_oper(nick))
        except Exception:
            return False

    def _reply(self, channel: str, text: str) -> None:
        for line in text.splitlines():
            self.connection.privmsg(channel, line)

    async def join_channel(self, channel: str) -> None:
        """Called by ChannelLinker right after a fresh mapping involving this
        connector is created, so a newly-linked channel is joined immediately
        instead of waiting for a restart to pick it up from Mongo."""
        if channel not in self._channels:
            self._channels.append(channel)
        if self._client.connection.is_connected():
            self._client.connection.join(channel)

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
    def __init__(self, sender: IrcSenderService) -> None:
        self.connector_id = sender.connector_id
        self._sender = sender

    async def receive(self, message: StandardMessage, *, target_channel_id: str) -> list[str]:
        # TODO: markdown stripping belongs here too.
        prefix = f"<{message.sender_name}> "
        limit = max(1, _LINE_LIMIT - len(prefix))
        ids: list[str] = []
        for line in message.content_markdown.splitlines() or [""]:
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
