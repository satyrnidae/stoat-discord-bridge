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
from types import SimpleNamespace

import pytest

from stoat_discord_bridge.config import IrcConnectorConfig
from stoat_discord_bridge.services.irc_service import IrcSenderService
from stoat_discord_bridge.status import HealthState, HealthTracker


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
        self.mode_calls: list[tuple[str, str]] = []
        self._connected = connected
        self._nickname = nickname

    def whois(self, targets) -> None:
        self.whois_calls.append(targets)

    def join(self, channel: str) -> None:
        self.join_calls.append(channel)

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


# ---------------------------------------------------------------- history-replay suppression


def _notice_event(channel: str, text: str):
    return SimpleNamespace(target=channel, arguments=[text])


def _pubmsg_event(channel: str, nick: str = "someone", text: str = "hi"):
    return SimpleNamespace(target=channel, arguments=[text], source=SimpleNamespace(nick=nick))


async def test_pubnotice_unrelated_text_is_ignored():
    sender = _make_sender()
    sender._handle_pubnotice(_notice_event("#general", "some other server notice"))
    assert sender._history_replay == {}


async def test_pubnotice_history_replay_sets_budget():
    sender = _make_sender()
    sender._handle_pubnotice(_notice_event("#general", "Replaying up to 50 lines of pre-join history from the last 5 years"))
    remaining, _deadline = sender._history_replay["#general"]
    assert remaining == 50


async def test_pubnotice_channel_key_is_case_insensitive():
    sender = _make_sender()
    sender._handle_pubnotice(_notice_event("#General", "Replaying up to 3 lines of pre-join history"))
    assert "#general" in sender._history_replay


async def test_consume_history_replay_suppresses_exactly_the_announced_count():
    sender = _make_sender()
    sender._handle_pubnotice(_notice_event("#general", "Replaying up to 2 lines of pre-join history"))

    assert sender._consume_history_replay("#general") is True
    assert sender._consume_history_replay("#general") is True
    assert sender._consume_history_replay("#general") is False  # budget exhausted - back to live


async def test_consume_history_replay_is_per_channel():
    sender = _make_sender()
    sender._handle_pubnotice(_notice_event("#general", "Replaying up to 1 lines of pre-join history"))

    assert sender._consume_history_replay("#other") is False  # different channel, no budget
    assert sender._consume_history_replay("#general") is True


async def test_consume_history_replay_expires_after_the_timeout(monkeypatch):
    sender = _make_sender()
    sender._handle_pubnotice(_notice_event("#general", "Replaying up to 50 lines of pre-join history"))

    # simulate the deadline having already passed, without a real sleep
    remaining, _ = sender._history_replay["#general"]
    sender._history_replay["#general"] = (remaining, 0.0)

    assert sender._consume_history_replay("#general") is False
    assert "#general" not in sender._history_replay


async def test_handle_pubmsg_suppresses_replayed_history_but_relays_live_messages():
    received = []

    async def record(message):
        received.append(message.content_markdown)

    sender = IrcSenderService(_irc_config(), [], on_message=record, health=HealthTracker({"irc": "IRC"}))
    sender._loop = asyncio.get_running_loop()

    sender._handle_pubnotice(_notice_event("#general", "Replaying up to 1 lines of pre-join history"))
    sender._handle_pubmsg(_pubmsg_event("#general", text="old replayed message"))
    sender._handle_pubmsg(_pubmsg_event("#general", text="a live message"))

    # run_coroutine_threadsafe schedules via call_soon_threadsafe, which
    # needs the loop to actually cycle - a bare sleep(0) isn't reliably
    # enough of a tick for that (unlike an in-loop create_task).
    await asyncio.sleep(0.05)

    assert received == ["a live message"]


# ---------------------------------------------------------------- join-blocked-on-+r retry
#
# Regression coverage for a real production bug: this bridge's target
# network (irc.satyrn.dev, InspIRCd) requires a registered+identified nick
# to join any channel but #welcome. _handle_welcome sends NickServ IDENTIFY
# and the initial JOINs back-to-back, with no wait for IDENTIFY to actually
# resolve - so on a fresh connect, the JOINs land before the server has
# processed IDENTIFY and get rejected with ERR_NEEDREGGEDNICK (numeric 477,
# dispatched as on_nochanmodes - see its docstring), and were never retried:
# the bridge looked "connected" (HealthTracker only tracked the raw IRC
# connection) while sitting outside every configured channel forever.


def _nochanmodes_event(channel: str, message: str = "You need to be identified to a registered account to join this channel."):
    return SimpleNamespace(arguments=[channel, message])


def _privnotice_event(nick: str, text: str = "Password accepted - you are now identified."):
    return SimpleNamespace(source=SimpleNamespace(nick=nick), arguments=[text])


def _join_event(nick: str, channel: str):
    return SimpleNamespace(source=SimpleNamespace(nick=nick), target=channel)


async def test_join_blocked_queues_the_channel_and_degrades_health():
    sender = _make_sender()

    sender._handle_join_blocked(_nochanmodes_event("#general"))

    assert sender._blocked_channels == {"#general"}
    assert sender._health.snapshot()["irc"] == HealthState.FAILING  # not yet marked connected in this test

    sender._health.mark_connected("irc")
    assert sender._health.snapshot()["irc"] == HealthState.DEGRADED


async def test_nickserv_privnotice_retries_every_blocked_channel_and_clears_the_queue(monkeypatch):
    sender = _make_sender()
    conn = FakeConnection()
    _patch_connection(monkeypatch, sender, conn)
    sender._handle_join_blocked(_nochanmodes_event("#general"))
    sender._handle_join_blocked(_nochanmodes_event("#other"))

    sender._handle_privnotice(_privnotice_event("NickServ"))

    assert set(conn.join_calls) == {"#general", "#other"}
    assert sender._blocked_channels == set()


async def test_nickserv_nick_check_is_case_insensitive():
    sender = _make_sender()
    conn = FakeConnection()
    sender._client.connection = conn
    sender._handle_join_blocked(_nochanmodes_event("#general"))

    sender._handle_privnotice(_privnotice_event("nickserv"))

    assert conn.join_calls == ["#general"]


async def test_privnotice_from_someone_else_does_not_retry(monkeypatch):
    sender = _make_sender()
    conn = FakeConnection()
    _patch_connection(monkeypatch, sender, conn)
    sender._handle_join_blocked(_nochanmodes_event("#general"))

    sender._handle_privnotice(_privnotice_event("SomeRandomUser"))

    assert conn.join_calls == []
    assert sender._blocked_channels == {"#general"}


async def test_retry_blocked_joins_is_a_noop_when_nothing_is_queued(monkeypatch):
    sender = _make_sender()
    conn = FakeConnection()
    _patch_connection(monkeypatch, sender, conn)

    sender._handle_privnotice(_privnotice_event("NickServ"))  # must not raise

    assert conn.join_calls == []


async def test_own_successful_join_clears_the_blocked_channel_and_records_a_health_success():
    sender = _make_sender()
    conn = FakeConnection(nickname="bot")
    sender._handle_join_blocked(_nochanmodes_event("#general"))
    sender._health.mark_connected("irc")
    assert sender._health.snapshot()["irc"] == HealthState.DEGRADED

    sender._handle_join(conn, _join_event("bot", "#general"))

    assert sender._blocked_channels == set()
    # a single success doesn't erase the earlier recorded failure from the
    # rolling window (see HealthTracker/_TargetHealth.state) - it stays
    # DEGRADED until enough later outcomes push that failure out, same as
    # any other connector's relay-error recovery. What matters here is that
    # a success was recorded at all, unlike before this fix (never was).
    assert sender._health._targets["irc"].recent_results[-1] is True


async def test_someone_elses_join_is_ignored():
    sender = _make_sender()
    conn = FakeConnection(nickname="bot")
    sender._handle_join_blocked(_nochanmodes_event("#general"))

    sender._handle_join(conn, _join_event("someone-else", "#general"))

    assert sender._blocked_channels == {"#general"}  # untouched - not our join
