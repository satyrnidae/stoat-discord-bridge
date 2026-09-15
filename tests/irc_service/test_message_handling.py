from __future__ import annotations

import asyncio
from types import SimpleNamespace

from stoat_discord_bridge.models import StandardMessage
from stoat_discord_bridge.services.irc_service import IrcSenderService
from stoat_discord_bridge.status import HealthState, HealthTracker
from tests.irc_service.conftest import FakeConnection, _irc_config, _make_sender, _patch_connection


# ---------------------------------------------------------------- history-replay suppression


def _notice_event(channel: str, text: str):
    return SimpleNamespace(target=channel, arguments=[text])


def _pubmsg_event(channel: str, nick: str = "someone", text: str = "hi"):
    return SimpleNamespace(target=channel, arguments=[text], source=SimpleNamespace(nick=nick))


def _msg(text: str = "hi") -> StandardMessage:
    return StandardMessage(
        origin_connector_id="irc",
        origin_channel_id="#general",
        channel_name="#general",
        sender_name="someone",
        sender_avatar_url=None,
        sender_user_id="someone",
        content_markdown=text,
        message_id="irc-test",
        attachments=[],
        source_label="IRC",
    )


async def test_pubnotice_unrelated_text_is_ignored():
    sender = _make_sender()
    sender._handle_pubnotice(_notice_event("#general", "some other server notice"))
    assert sender._history_replay == {}


async def test_pubnotice_history_replay_sets_budget():
    sender = _make_sender()
    sender._handle_pubnotice(_notice_event("#general", "Replaying up to 50 lines of pre-join history from the last 5 years"))
    assert sender._history_replay["#general"].remaining == 50


async def test_pubnotice_channel_key_is_case_insensitive():
    sender = _make_sender()
    sender._handle_pubnotice(_notice_event("#General", "Replaying up to 3 lines of pre-join history"))
    assert "#general" in sender._history_replay


async def test_consume_history_replay_suppresses_exactly_the_announced_count():
    sender = _make_sender()
    sender._handle_pubnotice(_notice_event("#general", "Replaying up to 2 lines of pre-join history"))

    assert sender._consume_history_replay("#general", _msg()) is True
    assert sender._consume_history_replay("#general", _msg()) is True
    assert sender._consume_history_replay("#general", _msg()) is False  # budget exhausted - back to live


async def test_consume_history_replay_is_per_channel():
    sender = _make_sender()
    sender._handle_pubnotice(_notice_event("#general", "Replaying up to 1 lines of pre-join history"))

    assert sender._consume_history_replay("#other", _msg()) is False  # different channel, no budget
    assert sender._consume_history_replay("#general", _msg()) is True


async def test_consume_history_replay_expires_after_the_timeout(monkeypatch):
    sender = _make_sender()
    sender._handle_pubnotice(_notice_event("#general", "Replaying up to 50 lines of pre-join history"))

    # simulate the deadline having already passed, without a real sleep
    sender._history_replay["#general"].deadline = 0.0

    assert sender._consume_history_replay("#general", _msg()) is False
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


async def test_handle_pubmsg_stamps_the_connector_label_as_source():
    received = []

    async def record(message):
        received.append(message)

    sender = IrcSenderService(
        _irc_config(label="IRC (satyrn)"), [], on_message=record, health=HealthTracker({"irc": "IRC"})
    )
    sender._loop = asyncio.get_running_loop()

    sender._handle_pubmsg(_pubmsg_event("#general", text="hi", nick="someone"))
    await asyncio.sleep(0.05)

    assert [m.source_label for m in received] == ["IRC (satyrn)"]


# ---------------------------------------------------------------- fetch_history (issue #141)
#
# IRC has no on-demand history query - chanhistory only replays as a side
# effect of JOIN - so fetch_history forces a fresh PART + re-JOIN and
# captures the replay burst that follows instead of dropping it the way the
# live-relay path (_consume_history_replay) does for an ordinary join.


async def test_fetch_history_parts_and_rejoins_then_captures_the_replay_burst():
    sender = _make_sender()
    conn = FakeConnection()
    sender._client.connection = conn

    task = asyncio.create_task(sender.fetch_history("#general", None))
    await asyncio.sleep(0)  # let fetch_history run up to the point it awaits the capture future

    assert conn.part_calls == ["#general"]
    assert conn.join_calls == ["#general"]

    sender._handle_pubnotice(_notice_event("#general", "Replaying up to 2 lines of pre-join history"))
    sender._handle_pubmsg(_pubmsg_event("#general", text="first"))
    sender._handle_pubmsg(_pubmsg_event("#general", text="second"))

    messages = await asyncio.wait_for(task, timeout=1)

    assert [m.content_markdown for m in messages] == ["first", "second"]
    assert sender._history_replay == {}
    assert sender._pending_history_capture == {}


async def test_fetch_history_does_not_relay_captured_messages_live():
    received = []

    async def record(message):
        received.append(message.content_markdown)

    sender = IrcSenderService(_irc_config(), [], on_message=record, health=HealthTracker({"irc": "IRC"}))
    sender._loop = asyncio.get_running_loop()
    conn = FakeConnection()
    sender._client.connection = conn

    task = asyncio.create_task(sender.fetch_history("#general", None))
    await asyncio.sleep(0)
    sender._handle_pubnotice(_notice_event("#general", "Replaying up to 1 lines of pre-join history"))
    sender._handle_pubmsg(_pubmsg_event("#general", text="captured"))
    await asyncio.wait_for(task, timeout=1)
    await asyncio.sleep(0.05)

    assert received == []  # captured for the fetch, not relayed as a live message


async def test_fetch_history_respects_limit_by_slicing_the_most_recent():
    sender = _make_sender()
    conn = FakeConnection()
    sender._client.connection = conn

    task = asyncio.create_task(sender.fetch_history("#general", 1))
    await asyncio.sleep(0)
    sender._handle_pubnotice(_notice_event("#general", "Replaying up to 3 lines of pre-join history"))
    sender._handle_pubmsg(_pubmsg_event("#general", text="first"))
    sender._handle_pubmsg(_pubmsg_event("#general", text="second"))
    sender._handle_pubmsg(_pubmsg_event("#general", text="third"))

    messages = await asyncio.wait_for(task, timeout=1)

    assert [m.content_markdown for m in messages] == ["third"]


async def test_fetch_history_resolves_empty_when_no_replay_notice_arrives(monkeypatch):
    import stoat_discord_bridge.services.irc_service.sender as irc_sender_module

    monkeypatch.setattr(irc_sender_module, "_HISTORY_FETCH_TIMEOUT", 0.05)
    sender = _make_sender()
    conn = FakeConnection()
    sender._client.connection = conn

    messages = await asyncio.wait_for(sender.fetch_history("#general", None), timeout=1)

    assert messages == []
    assert sender._history_replay == {}
    assert sender._pending_history_capture == {}


async def test_fetch_history_on_disconnected_connection_resolves_empty():
    sender = _make_sender()
    conn = FakeConnection(connected=False)
    sender._client.connection = conn

    messages = await sender.fetch_history("#general", None)

    assert messages == []
    assert conn.part_calls == []
    assert conn.join_calls == []


async def test_fetch_history_serializes_concurrent_calls_on_the_same_channel():
    # A second fetch_history call for the same channel while the first is
    # still awaiting its replay must not clobber the first call's pending
    # capture state (issue #141 code-review catch) - it waits for the first
    # PART/re-JOIN cycle to fully resolve before issuing its own.
    sender = _make_sender()
    conn = FakeConnection()
    sender._client.connection = conn

    first = asyncio.create_task(sender.fetch_history("#general", None))
    await asyncio.sleep(0)
    second = asyncio.create_task(sender.fetch_history("#general", None))
    await asyncio.sleep(0)

    # only the first call's PART/JOIN has fired yet - the second is waiting on the lock
    assert conn.part_calls == ["#general"]
    assert conn.join_calls == ["#general"]

    sender._handle_pubnotice(_notice_event("#general", "Replaying up to 1 lines of pre-join history"))
    sender._handle_pubmsg(_pubmsg_event("#general", text="first-call"))
    first_messages = await asyncio.wait_for(first, timeout=1)
    assert [m.content_markdown for m in first_messages] == ["first-call"]

    await asyncio.sleep(0)  # let the second call acquire the lock and issue its own PART/JOIN
    assert conn.part_calls == ["#general", "#general"]
    assert conn.join_calls == ["#general", "#general"]

    sender._handle_pubnotice(_notice_event("#general", "Replaying up to 1 lines of pre-join history"))
    sender._handle_pubmsg(_pubmsg_event("#general", text="second-call"))
    second_messages = await asyncio.wait_for(second, timeout=1)
    assert [m.content_markdown for m in second_messages] == ["second-call"]


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
