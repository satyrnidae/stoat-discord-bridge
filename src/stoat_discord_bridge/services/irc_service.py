"""IRC sender/receiver services (irc.satyrn.dev, Tethys IRCd).

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

from stoat_discord_bridge.config import IrcConfig
from stoat_discord_bridge.models import Platform, StandardMessage
from stoat_discord_bridge.services.base import OnMessage, PartialRelayError, ReceiverService, SenderService
from stoat_discord_bridge.services.formatting import chunk_content
from stoat_discord_bridge.status import HealthTracker

# IRC's protocol limit is 512 bytes per raw line, including the
# `:nick!user@host PRIVMSG #channel :` prefix the server sees and the
# trailing CRLF. We don't know our own hostmask as the server will render it,
# so this leaves a generous margin for that plus the "<sender_name> " prefix
# this receiver prepends to each line.
_LINE_LIMIT = 400


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

    def __init__(self, owner: IrcSenderService, config: IrcConfig) -> None:
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
    platform = Platform.IRC

    def __init__(self, config: IrcConfig, channels: list[str], on_message: OnMessage, health: HealthTracker) -> None:
        SenderService.__init__(self, on_message)
        self._config = config
        self._channels = channels
        self._health = health
        self._client = _IrcClient(self, config)
        self._loop: asyncio.AbstractEventLoop | None = None

    @property
    def connection(self):
        return self._client.connection

    def _handle_welcome(self, connection) -> None:
        self._health.mark_connected(self.platform)
        if self._config.nickserv_password:
            connection.privmsg("NickServ", f"IDENTIFY {self._config.nickserv_password}")
        for channel in self._channels:
            connection.join(channel)

    def _handle_disconnect(self) -> None:
        self._health.mark_disconnected(self.platform)

    def _handle_privmsg(self, connection, event) -> None:
        # DM to the bot. `STATUS` reports sync target health; anything else
        # is ignored (IRC has no other DM-driven commands yet).
        if event.arguments[0].strip().upper() == "STATUS":
            for line in self._health.render().splitlines():
                connection.notice(event.source.nick, line)

    def _handle_pubmsg(self, event) -> None:
        channel = event.target
        content = event.arguments[0]
        self._schedule(
            self._on_message(
                StandardMessage(
                    origin_platform=Platform.IRC,
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
    platform = Platform.IRC

    def __init__(self, sender: IrcSenderService) -> None:
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
