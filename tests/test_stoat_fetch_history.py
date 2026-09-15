"""Tests for StoatSenderService.fetch_history - the Stoat half of the
`ConnectorInfo.fetch_history` hook behind `/mirror channel with history`
(issue #122). Hand-rolls pagination over stoat.py's <=100-per-call
`TextChannel.history(after=, sort=oldest)`, converting each page via the
same `_to_standard_message` the live relay path uses.
"""

from __future__ import annotations

import stoat

from tests.fakes.fake_stoat import FakeAuthor, FakeChannel, FakeClient
from tests.test_stoat_sender_dispatch import _Recorder, _make_sender, _stoat_message


async def test_fetch_history_converts_messages_oldest_first():
    channel = FakeChannel(id="42", name="general")
    channel.set_history(
        [
            _stoat_message(channel=channel, author=FakeAuthor(id="u1", display_name="Alice"), content="first", id="m1"),
            _stoat_message(channel=channel, author=FakeAuthor(id="u1", display_name="Alice"), content="second", id="m2"),
        ]
    )
    client = FakeClient()
    client.add_channel(channel)
    sender = _make_sender(_Recorder(), client)

    messages = await sender.fetch_history("42", None)

    assert [m.content_markdown for m in messages] == ["first", "second"]
    assert messages[0].origin_connector_id == "stoat"
    assert messages[0].message_id == "m1"


async def test_fetch_history_respects_a_numeric_limit():
    # A numeric limit means the N *most recent* messages (issue #122), not
    # the oldest N - still returned oldest-first so relay order is correct.
    channel = FakeChannel(id="42", name="general")
    channel.set_history(
        [
            _stoat_message(channel=channel, author=FakeAuthor(id="u1"), content=f"m{i}", id=f"id{i}")
            for i in range(1, 6)
        ]
    )
    client = FakeClient()
    client.add_channel(channel)
    sender = _make_sender(_Recorder(), client)

    messages = await sender.fetch_history("42", 2)

    assert [m.content_markdown for m in messages] == ["m4", "m5"]


async def test_fetch_history_numeric_limit_doesnt_count_filtered_messages():
    # The most recent raw message is a non-whitelisted bot post that gets
    # filtered out - it must not count against `limit`, so the backfill
    # still walks back far enough to find 2 *real* messages.
    channel = FakeChannel(id="42", name="general")
    channel.set_history(
        [
            _stoat_message(channel=channel, author=FakeAuthor(id="u1"), content="m1", id="id1"),
            _stoat_message(channel=channel, author=FakeAuthor(id="u1"), content="m2", id="id2"),
            _stoat_message(channel=channel, author=FakeAuthor(id="u1"), content="m3", id="id3"),
            _stoat_message(channel=channel, author=FakeAuthor(id="u1"), content="m4", id="id4"),
            _stoat_message(channel=channel, author=FakeAuthor(id="bot1", bot=True), content="beep", id="id5"),
        ]
    )
    client = FakeClient()
    client.add_channel(channel)
    sender = _make_sender(_Recorder(), client)

    messages = await sender.fetch_history("42", 2)

    assert [m.content_markdown for m in messages] == ["m3", "m4"]


async def test_fetch_history_paginates_past_the_100_per_call_cap():
    channel = FakeChannel(id="42", name="general")
    channel.set_history(
        [
            _stoat_message(channel=channel, author=FakeAuthor(id="u1"), content=f"m{i}", id=f"id{i:03d}")
            for i in range(150)
        ]
    )
    client = FakeClient()
    client.add_channel(channel)
    sender = _make_sender(_Recorder(), client)

    messages = await sender.fetch_history("42", None)

    assert len(messages) == 150
    assert [m.content_markdown for m in messages] == [f"m{i}" for i in range(150)]


async def test_fetch_history_drops_the_bridges_own_masqueraded_messages():
    channel = FakeChannel(id="42", name="general")
    channel.set_history(
        [_stoat_message(channel=channel, author=FakeAuthor(id="bridge-bot-id"), content="relayed", id="m1")]
    )
    client = FakeClient()
    client.add_channel(channel)
    sender = _make_sender(_Recorder(), client, self_id="bridge-bot-id")

    messages = await sender.fetch_history("42", None)

    assert messages == []


async def test_fetch_history_drops_a_non_whitelisted_bot_author():
    channel = FakeChannel(id="42", name="general")
    channel.set_history(
        [_stoat_message(channel=channel, author=FakeAuthor(id="u1", bot=True), content="beep", id="m1")]
    )
    client = FakeClient()
    client.add_channel(channel)
    sender = _make_sender(_Recorder(), client)

    messages = await sender.fetch_history("42", None)

    assert messages == []


async def test_fetch_history_drops_system_event_rows():
    channel = FakeChannel(id="42", name="general")
    pin_row = _stoat_message(channel=channel, author=FakeAuthor(id="u1"), content="", id="sys1")
    pin_row.system_event = stoat.MessagePinnedSystemEvent(pinned_message_id="pm1", internal_by="u1", message=None)
    real_row = _stoat_message(channel=channel, author=FakeAuthor(id="u1"), content="real one", id="m1")
    channel.set_history([pin_row, real_row])
    client = FakeClient()
    client.add_channel(channel)
    sender = _make_sender(_Recorder(), client)

    messages = await sender.fetch_history("42", None)

    assert [m.content_markdown for m in messages] == ["real one"]


async def test_fetch_history_returns_empty_for_an_unresolvable_channel():
    client = FakeClient()
    sender = _make_sender(_Recorder(), client)

    messages = await sender.fetch_history("999999", None)

    assert messages == []


async def test_fetch_history_paces_between_page_fetches_but_not_before_the_first(monkeypatch):
    sleeps = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr("stoat_discord_bridge.services.stoat_service.sender.asyncio.sleep", fake_sleep)
    channel = FakeChannel(id="42", name="general")
    channel.set_history(
        [
            _stoat_message(channel=channel, author=FakeAuthor(id="u1"), content=f"m{i}", id=f"id{i:03d}")
            for i in range(150)
        ]
    )
    client = FakeClient()
    client.add_channel(channel)
    sender = _make_sender(_Recorder(), client)

    await sender.fetch_history("42", None)

    # 150 messages over the 100-per-page cap is 2 page fetches - pacing
    # applies only between them, never before the first.
    assert len(sleeps) == 1
