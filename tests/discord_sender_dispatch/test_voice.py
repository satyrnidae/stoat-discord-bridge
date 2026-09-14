from __future__ import annotations

from types import SimpleNamespace

from tests.discord_sender_dispatch.conftest import _Recorder, _make_sender
from tests.fakes.fake_discord import FakeChannel, FakeClient, FakeGuild, FakeUser, FakeVoiceChannel

# ---------------------------------------------------------------- channel_is_voice


async def test_channel_is_voice_true_for_a_voice_channel():
    client = FakeClient()
    client.add_channel(FakeVoiceChannel(id=42, name="Lounge"))
    sender = _make_sender(_Recorder(), client)

    assert await sender.channel_is_voice("42") is True


async def test_channel_is_voice_false_for_a_text_channel():
    client = FakeClient()
    client.add_channel(FakeChannel(id=42, name="general"))
    sender = _make_sender(_Recorder(), client)

    assert await sender.channel_is_voice("42") is False


async def test_channel_is_voice_none_for_an_unresolvable_id():
    sender = _make_sender(_Recorder(), FakeClient())

    assert await sender.channel_is_voice("999") is None
    assert await sender.channel_is_voice("not-a-number") is None


# ---------------------------------------------------------------- voice_occupants


async def test_voice_occupants_excludes_bots():
    client = FakeClient()
    human = FakeUser(id=1, display_name="human", bot=False)
    bot = FakeUser(id=2, display_name="bot", bot=True)
    client.add_channel(FakeVoiceChannel(id=42, name="Lounge", members=[human, bot]))
    sender = _make_sender(_Recorder(), client)

    assert await sender.voice_occupants("42") == {"1"}


async def test_voice_occupants_none_for_an_unresolvable_id():
    sender = _make_sender(_Recorder(), FakeClient())

    assert await sender.voice_occupants("999") is None


# ---------------------------------------------------------------- _handle_voice_state_update


async def test_voice_state_update_reports_join():
    recorder = _Recorder()
    sender = _make_sender(recorder, FakeClient())
    guild = FakeGuild(id=123)
    channel = FakeVoiceChannel(id=42, guild=guild)
    member = FakeUser(id=7, bot=False)

    await sender._handle_voice_state_update(
        member, SimpleNamespace(channel=None), SimpleNamespace(channel=channel)
    )

    assert recorder.voice_presence == [("discord", "42", "7", True, False)]


async def test_voice_state_update_reports_leave():
    recorder = _Recorder()
    sender = _make_sender(recorder, FakeClient())
    channel = FakeVoiceChannel(id=42)
    member = FakeUser(id=7, bot=True)

    await sender._handle_voice_state_update(
        member, SimpleNamespace(channel=channel), SimpleNamespace(channel=None)
    )

    assert recorder.voice_presence == [("discord", "42", "7", False, True)]


async def test_voice_state_update_reports_move_as_leave_then_join():
    recorder = _Recorder()
    sender = _make_sender(recorder, FakeClient())
    old_channel = FakeVoiceChannel(id=42)
    new_channel = FakeVoiceChannel(id=43)
    member = FakeUser(id=7, bot=False)

    await sender._handle_voice_state_update(
        member, SimpleNamespace(channel=old_channel), SimpleNamespace(channel=new_channel)
    )

    assert recorder.voice_presence == [
        ("discord", "42", "7", False, False),
        ("discord", "43", "7", True, False),
    ]


async def test_voice_state_update_no_change_reports_nothing():
    recorder = _Recorder()
    sender = _make_sender(recorder, FakeClient())
    channel = FakeVoiceChannel(id=42)
    member = FakeUser(id=7, bot=False)

    await sender._handle_voice_state_update(
        member, SimpleNamespace(channel=channel), SimpleNamespace(channel=channel)
    )

    assert recorder.voice_presence == []


# ---------------------------------------------------------------- _handle_disconnect


async def test_handle_disconnect_reports_voice_connector_lost():
    recorder = _Recorder()
    sender = _make_sender(recorder, FakeClient())

    await sender._handle_disconnect()

    assert recorder.voice_connector_lost == ["discord"]


async def test_handle_disconnect_is_a_noop_when_voice_connector_lost_unwired():
    sender = _make_sender(_Recorder(), FakeClient())
    sender._on_voice_connector_lost = None

    await sender._handle_disconnect()  # must not raise
