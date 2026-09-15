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


# ---------------------------------------------------------------- call-started notice (issue #154)


async def test_voice_state_update_relays_a_call_started_notice_for_the_first_joiner():
    recorder = _Recorder()
    sender = _make_sender(recorder, FakeClient())
    channel = FakeVoiceChannel(id=42, name="Lounge")
    member = FakeUser(id=7, display_name="Alice", bot=False)

    await sender._handle_voice_state_update(
        member, SimpleNamespace(channel=None), SimpleNamespace(channel=channel)
    )

    [notice] = recorder.messages
    assert notice.origin_connector_id == "discord"
    assert notice.origin_channel_id == "42"
    assert notice.content_markdown == "<@7> started a call in Discord"
    assert notice.mentioned_users == {"7": "Alice"}
    assert notice.sender_name == "Bridge"
    assert notice.sender_user_id == "1"


async def test_voice_state_update_no_notice_when_channel_already_occupied():
    recorder = _Recorder()
    sender = _make_sender(recorder, FakeClient())
    other = FakeUser(id=9, display_name="Bob", bot=False)
    channel = FakeVoiceChannel(id=42, members=[other])
    member = FakeUser(id=7, bot=False)

    await sender._handle_voice_state_update(
        member, SimpleNamespace(channel=None), SimpleNamespace(channel=channel)
    )

    assert recorder.messages == []


async def test_voice_state_update_no_notice_when_only_bots_were_present():
    recorder = _Recorder()
    sender = _make_sender(recorder, FakeClient())
    a_bot = FakeUser(id=9, display_name="Bot", bot=True)
    channel = FakeVoiceChannel(id=42, members=[a_bot])
    member = FakeUser(id=7, bot=False)

    await sender._handle_voice_state_update(
        member, SimpleNamespace(channel=None), SimpleNamespace(channel=channel)
    )

    [notice] = recorder.messages
    assert notice.content_markdown == "<@7> started a call in Discord"


async def test_voice_state_update_no_notice_for_a_bot_joining():
    recorder = _Recorder()
    sender = _make_sender(recorder, FakeClient())
    channel = FakeVoiceChannel(id=42)
    member = FakeUser(id=7, bot=True)

    await sender._handle_voice_state_update(
        member, SimpleNamespace(channel=None), SimpleNamespace(channel=channel)
    )

    assert recorder.messages == []


async def test_voice_state_update_move_only_checks_the_new_channels_occupancy():
    # The member is leaving a populated channel (old_channel) and landing in
    # an empty one (new_channel) - the notice cares only about whether the
    # *destination* was empty, not what they left behind.
    recorder = _Recorder()
    sender = _make_sender(recorder, FakeClient())
    old_channel = FakeVoiceChannel(id=42)
    new_channel = FakeVoiceChannel(id=43)
    member = FakeUser(id=7, bot=False)

    await sender._handle_voice_state_update(
        member, SimpleNamespace(channel=old_channel), SimpleNamespace(channel=new_channel)
    )

    [notice] = recorder.messages
    assert notice.origin_channel_id == "43"


async def test_voice_state_update_no_notice_when_voice_presence_is_unwired():
    recorder = _Recorder()
    sender = _make_sender(recorder, FakeClient())
    sender._on_voice_presence = None
    channel = FakeVoiceChannel(id=42)
    member = FakeUser(id=7, bot=False)

    await sender._handle_voice_state_update(
        member, SimpleNamespace(channel=None), SimpleNamespace(channel=channel)
    )

    assert recorder.messages == []


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
