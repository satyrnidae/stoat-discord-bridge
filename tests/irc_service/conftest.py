"""Shared fixtures for IrcSenderService tests (tests/irc_service/) that
don't require an actual network connection. Constructing an IrcSenderService
itself doesn't touch the network (the underlying irc.bot.SingleServerIRCBot
only connects on start()), so these tests build a real instance and
monkeypatch just its `connection` property to a fake that records calls
instead of hitting a socket.
"""

from __future__ import annotations

import asyncio

from stoat_discord_bridge.config import IrcConnectorConfig
from stoat_discord_bridge.services.irc_service import IrcSenderService
from stoat_discord_bridge.status import HealthTracker

__all__ = ["_irc_config", "_noop", "_make_sender", "FakeConnection", "_patch_connection"]


def _irc_config(**overrides):
    defaults = dict(
        id="irc", label="IRC", host="irc.example.invalid", port=6697, use_tls=True, nick="bot",
        nickserv_password=None, default_channel_modes=None, ident=None, oper_account=None, oper_password=None,
        client_cert_file=None, client_key_file=None,
    )
    defaults.update(overrides)
    return IrcConnectorConfig(**defaults)


async def _noop(_message) -> None:
    pass


def _make_sender(**config_overrides) -> IrcSenderService:
    sender = IrcSenderService(
        _irc_config(**config_overrides), [], on_message=_noop, health=HealthTracker({"irc": "IRC"})
    )
    sender._loop = asyncio.get_running_loop()  # normally set by start()
    return sender


class FakeConnection:
    def __init__(self, connected: bool = True, nickname: str = "bot"):
        self.whois_calls: list = []
        self.join_calls: list[str] = []
        self.part_calls: list[str] = []
        self.privmsg_calls: list[tuple[str, str]] = []
        self.mode_calls: list[tuple[str, str]] = []
        self.topic_calls: list[tuple[str, str]] = []
        self._connected = connected
        self._nickname = nickname

    def topic(self, channel: str, new_topic: str | None = None) -> None:
        self.topic_calls.append((channel, new_topic))

    def whois(self, targets) -> None:
        self.whois_calls.append(targets)

    def join(self, channel: str) -> None:
        self.join_calls.append(channel)

    def part(self, channel: str, message: str = "") -> None:
        self.part_calls.append(channel)

    def privmsg(self, target: str, text: str) -> None:
        self.privmsg_calls.append((target, text))

    def mode(self, channel: str, modes: str) -> None:
        self.mode_calls.append((channel, modes))

    def is_connected(self) -> bool:
        return self._connected

    def get_nickname(self) -> str:
        return self._nickname


def _patch_connection(monkeypatch, sender, connection: FakeConnection) -> None:
    # IrcSenderService.connection is a read-only property proxying
    # self._client.connection - some methods (join_channel, ensure_channel)
    # read straight through self._client.connection instead of the
    # property, so patch the real underlying (plain, settable) attribute
    # rather than the property itself.
    monkeypatch.setattr(sender._client, "connection", connection)
