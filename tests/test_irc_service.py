"""Tests for the pieces of IrcSenderService that don't require an actual
network connection: the WHOIS-based oper-status check (the trickiest part
of the whole module - a Future resolved from a different thread than the
one awaiting it, see _check_is_oper/_resolve_whois's own comments) and the
default_channel_modes / ensure_channel logic in join_channel.

Constructing an IrcSenderService itself doesn't touch the network (the
underlying irc.bot.SingleServerIRCBot only connects on start()), so these
tests build a real instance and monkeypatch just its `connection` property
to a fake that records calls instead of hitting a socket.
"""

from __future__ import annotations

import asyncio

import pytest

from stoat_discord_bridge.config import IrcConnectorConfig
from stoat_discord_bridge.services.irc_service import IrcSenderService
from stoat_discord_bridge.status import HealthTracker


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
    def __init__(self, connected: bool = True):
        self.whois_calls: list = []
        self.join_calls: list[str] = []
        self.mode_calls: list[tuple[str, str]] = []
        self._connected = connected

    def whois(self, targets) -> None:
        self.whois_calls.append(targets)

    def join(self, channel: str) -> None:
        self.join_calls.append(channel)

    def mode(self, channel: str, modes: str) -> None:
        self.mode_calls.append((channel, modes))

    def is_connected(self) -> bool:
        return self._connected


def _patch_connection(monkeypatch, sender, connection: FakeConnection) -> None:
    # IrcSenderService.connection is a read-only property proxying
    # self._client.connection - some methods (join_channel, ensure_channel)
    # read straight through self._client.connection instead of the
    # property, so patch the real underlying (plain, settable) attribute
    # rather than the property itself.
    monkeypatch.setattr(sender._client, "connection", connection)


# ---------------------------------------------------------------- _check_is_oper / _resolve_whois


async def test_check_is_oper_true_on_whoisoperator(monkeypatch):
    sender = _make_sender()
    conn = FakeConnection()
    _patch_connection(monkeypatch, sender, conn)

    task = asyncio.create_task(sender._check_is_oper("Alice"))
    await asyncio.sleep(0)  # let _check_is_oper run up to awaiting the pending future
    sender._resolve_whois("Alice", True)

    assert await task is True
    assert conn.whois_calls == [["Alice"]]


async def test_check_is_oper_false_on_endofwhois_alone(monkeypatch):
    sender = _make_sender()
    _patch_connection(monkeypatch, sender, FakeConnection())

    task = asyncio.create_task(sender._check_is_oper("Bob"))
    await asyncio.sleep(0)
    sender._resolve_whois("Bob", False)  # on_endofwhois, no prior on_whoisoperator

    assert await task is False


async def test_check_is_oper_true_survives_a_trailing_endofwhois_false(monkeypatch):
    """on_whoisoperator (True) always arrives before on_endofwhois (False)
    for the same exchange when the target IS an oper - the trailing False
    call must be a no-op, not overwrite the already-resolved True."""
    sender = _make_sender()
    _patch_connection(monkeypatch, sender, FakeConnection())

    task = asyncio.create_task(sender._check_is_oper("Carol"))
    await asyncio.sleep(0)
    sender._resolve_whois("Carol", True)
    sender._resolve_whois("Carol", False)  # trailing on_endofwhois - must not clobber True

    assert await task is True


async def test_check_is_oper_nick_matching_is_case_insensitive(monkeypatch):
    sender = _make_sender()
    _patch_connection(monkeypatch, sender, FakeConnection())

    task = asyncio.create_task(sender._check_is_oper("DAVE"))
    await asyncio.sleep(0)
    sender._resolve_whois("dave", True)  # server echoes a differently-cased nick

    assert await task is True


async def test_resolve_whois_with_no_pending_future_is_a_noop(monkeypatch):
    sender = _make_sender()
    _patch_connection(monkeypatch, sender, FakeConnection())
    sender._resolve_whois("NoOneAsked", True)  # must not raise


async def test_check_is_oper_cleans_up_pending_entry(monkeypatch):
    sender = _make_sender()
    _patch_connection(monkeypatch, sender, FakeConnection())

    task = asyncio.create_task(sender._check_is_oper("Eve"))
    await asyncio.sleep(0)
    assert "eve" in sender._pending_whois
    sender._resolve_whois("Eve", True)
    await task
    assert "eve" not in sender._pending_whois


# ---------------------------------------------------------------- join_channel / default_channel_modes


async def test_join_channel_applies_default_modes_to_a_new_channel(monkeypatch):
    sender = _make_sender(default_channel_modes="+nt")
    conn = FakeConnection()
    _patch_connection(monkeypatch, sender, conn)

    await sender.join_channel("#newchan")

    assert conn.join_calls == ["#newchan"]
    assert conn.mode_calls == [("#newchan", "+nt")]


async def test_join_channel_skips_default_modes_on_a_channel_already_known(monkeypatch):
    sender = _make_sender(default_channel_modes="+nt")
    sender._channels.append("#existing")
    conn = FakeConnection()
    _patch_connection(monkeypatch, sender, conn)

    await sender.join_channel("#existing")

    assert conn.join_calls == ["#existing"]
    assert conn.mode_calls == []  # not "new" this session - no MODE sent


async def test_join_channel_no_modes_configured_sends_no_mode(monkeypatch):
    sender = _make_sender(default_channel_modes=None)
    conn = FakeConnection()
    _patch_connection(monkeypatch, sender, conn)

    await sender.join_channel("#newchan")

    assert conn.mode_calls == []


async def test_join_channel_does_nothing_while_disconnected(monkeypatch):
    sender = _make_sender(default_channel_modes="+nt")
    conn = FakeConnection(connected=False)
    _patch_connection(monkeypatch, sender, conn)

    await sender.join_channel("#newchan")

    assert conn.join_calls == []
    assert conn.mode_calls == []
    assert "#newchan" in sender._channels  # still tracked for a later reconnect


# ---------------------------------------------------------------- ensure_channel


async def test_ensure_channel_adds_hash_prefix_if_missing(monkeypatch):
    sender = _make_sender()
    conn = FakeConnection()
    _patch_connection(monkeypatch, sender, conn)

    result = await sender.ensure_channel("general")

    assert result == "#general"
    assert conn.join_calls == ["#general"]


async def test_ensure_channel_leaves_existing_hash_prefix_alone(monkeypatch):
    sender = _make_sender()
    conn = FakeConnection()
    _patch_connection(monkeypatch, sender, conn)

    result = await sender.ensure_channel("#general")

    assert result == "#general"
    assert conn.join_calls == ["#general"]
