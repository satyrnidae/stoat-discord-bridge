from __future__ import annotations

from types import SimpleNamespace

from tests.fakes.fake_stoat import FakeAuthor, FakeChannel, FakeClient, FakeVoiceChannel
from tests.stoat_service.conftest import _make_sender

# ---------------------------------------------------------------- channel_is_voice


async def test_channel_is_voice_true_for_a_voice_channel():
    client = FakeClient()
    client.add_channel(FakeVoiceChannel(id="42", name="Lounge"))
    sender = _make_sender(client=client)

    assert await sender.channel_is_voice("42") is True


async def test_channel_is_voice_false_for_a_text_channel():
    client = FakeClient()
    client.add_channel(FakeChannel(id="42", name="general"))
    sender = _make_sender(client=client)

    assert await sender.channel_is_voice("42") is False


async def test_channel_is_voice_none_for_an_unresolvable_id():
    sender = _make_sender(client=FakeClient())

    assert await sender.channel_is_voice("999") is None


# ---------------------------------------------------------------- voice_occupants


async def test_voice_occupants_excludes_bots():
    client = FakeClient()
    client.add_user("1", FakeAuthor(id="1", bot=False))
    client.add_user("2", FakeAuthor(id="2", bot=True))
    client.add_channel(
        FakeVoiceChannel(id="42", participants={"1": SimpleNamespace(user_id="1"), "2": SimpleNamespace(user_id="2")})
    )
    sender = _make_sender(client=client)

    assert await sender.voice_occupants("42") == {"1"}


async def test_voice_occupants_none_for_an_unresolvable_id():
    sender = _make_sender(client=FakeClient())

    assert await sender.voice_occupants("999") is None


async def test_voice_occupants_empty_set_for_an_empty_voice_channel():
    client = FakeClient()
    client.add_channel(FakeVoiceChannel(id="42"))
    sender = _make_sender(client=client)

    assert await sender.voice_occupants("42") == set()


# ---------------------------------------------------------------- presence push events


async def test_voice_channel_join_reports_presence():
    calls: list = []

    async def on_voice_presence(*args, **kwargs):
        calls.append((args, kwargs))

    client = FakeClient()
    client.add_user("7", FakeAuthor(id="7", bot=False))
    sender = _make_sender(client=client, on_voice_presence=on_voice_presence)

    event = SimpleNamespace(channel_id="42", state=SimpleNamespace(user_id="7"))
    await sender._handle_voice_channel_join(event)

    assert calls == [(("stoat", "42", "7"), {"present": True, "is_bot": False})]


async def test_voice_channel_leave_reports_presence_and_bot_flag():
    calls: list = []

    async def on_voice_presence(*args, **kwargs):
        calls.append((args, kwargs))

    client = FakeClient()
    client.add_user("7", FakeAuthor(id="7", bot=True))
    sender = _make_sender(client=client, on_voice_presence=on_voice_presence)

    event = SimpleNamespace(channel_id="42", user_id="7")
    await sender._handle_voice_channel_leave(event)

    assert calls == [(("stoat", "42", "7"), {"present": False, "is_bot": True})]


async def test_voice_channel_move_reports_leave_then_join():
    calls: list = []

    async def on_voice_presence(*args, **kwargs):
        calls.append((args, kwargs))

    client = FakeClient()
    client.add_user("7", FakeAuthor(id="7", bot=False))
    sender = _make_sender(client=client, on_voice_presence=on_voice_presence)

    event = SimpleNamespace(user_id="7", from_="42", to="43")
    await sender._handle_voice_channel_move(event)

    assert calls == [
        (("stoat", "42", "7"), {"present": False, "is_bot": False}),
        (("stoat", "43", "7"), {"present": True, "is_bot": False}),
    ]


async def test_voice_presence_handlers_are_no_ops_when_not_wired():
    sender = _make_sender(client=FakeClient())  # on_voice_presence defaults to None

    await sender._handle_voice_channel_join(SimpleNamespace(channel_id="42", state=SimpleNamespace(user_id="7")))
    await sender._handle_voice_channel_leave(SimpleNamespace(channel_id="42", user_id="7"))
    await sender._handle_voice_channel_move(SimpleNamespace(user_id="7", from_="42", to="43"))
    # no exception raised is the assertion
