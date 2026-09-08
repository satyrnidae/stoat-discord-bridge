from __future__ import annotations

from types import SimpleNamespace

from tests.fakes.fake_discord import (
    FakeChannel,
    FakeClient,
    FakeGuild,
    FakePartialEmoji,
    FakeRawReactionActionEvent,
    FakeUser,
)
from tests.discord_sender_dispatch.conftest import _Recorder, _make_sender


# ---------------------------------------------------------------- _handle_typing


def _typing_channel(id=42, guild_id=123):
    return SimpleNamespace(id=id, guild=FakeGuild(id=guild_id))


async def test_handle_typing_emits_a_standard_typing():
    recorder = _Recorder()
    sender = _make_sender(recorder, FakeClient())

    await sender._handle_typing(_typing_channel(), FakeUser(id=7, display_name="Alice"))

    assert [(t.origin_channel_id, t.sender_name, t.sender_user_id) for t in recorder.typing] == [
        ("42", "Alice", "7")
    ]


async def test_handle_typing_ignores_dms_and_other_guilds():
    recorder = _Recorder()
    sender = _make_sender(recorder, FakeClient())

    await sender._handle_typing(SimpleNamespace(id=42, guild=None), FakeUser(id=7))
    await sender._handle_typing(_typing_channel(guild_id=999), FakeUser(id=7))

    assert recorder.typing == []


async def test_handle_typing_ignores_the_bridge_bot_and_other_bots():
    recorder = _Recorder()
    client = FakeClient(user=FakeUser(id=1))
    sender = _make_sender(recorder, client)

    await sender._handle_typing(_typing_channel(), FakeUser(id=1))  # the bridge bot itself
    await sender._handle_typing(_typing_channel(), FakeUser(id=2, bot=True))

    assert recorder.typing == []


# ---------------------------------------------------------------- _handle_raw_reaction


def _reaction_payload(**overrides):
    defaults = dict(
        guild_id=123,
        channel_id=42,
        message_id=7,
        user_id=2,
        emoji=FakePartialEmoji(name="\U0001f600"),
        member=None,
    )
    defaults.update(overrides)
    return FakeRawReactionActionEvent(**defaults)


async def test_handle_raw_reaction_dispatches_add_and_remove():
    recorder = _Recorder()
    client = FakeClient(user=FakeUser(id=1))
    sender = _make_sender(recorder, client)

    await sender._handle_raw_reaction(_reaction_payload(), added=True)
    await sender._handle_raw_reaction(_reaction_payload(), added=False)

    assert [r.added for r in recorder.reactions] == [True, False]
    assert recorder.reactions[0].origin_channel_id == "42"
    assert recorder.reactions[0].origin_message_id == "7"
    assert recorder.reactions[0].emoji == "\U0001f600"


async def test_handle_raw_reaction_ignores_a_different_guild():
    recorder = _Recorder()
    client = FakeClient(user=FakeUser(id=1))
    sender = _make_sender(recorder, client)

    await sender._handle_raw_reaction(_reaction_payload(guild_id=999), added=True)

    assert recorder.reactions == []


async def test_handle_raw_reaction_drops_the_bridges_own_mirrored_reaction():
    recorder = _Recorder()
    client = FakeClient(user=FakeUser(id=1))
    sender = _make_sender(recorder, client)

    await sender._handle_raw_reaction(_reaction_payload(user_id=1), added=True)

    assert recorder.reactions == []


async def test_handle_raw_reaction_drops_another_bots_reaction_via_member():
    recorder = _Recorder()
    client = FakeClient(user=FakeUser(id=1))
    sender = _make_sender(recorder, client)

    await sender._handle_raw_reaction(_reaction_payload(member=FakeUser(id=5, bot=True)), added=True)

    assert recorder.reactions == []


async def test_handle_raw_reaction_drops_another_bots_reaction_removal_via_user_cache():
    recorder = _Recorder()
    client = FakeClient(user=FakeUser(id=1))
    client.add_user(FakeUser(id=5, bot=True))
    sender = _make_sender(recorder, client)

    # REACTION_REMOVE never carries `member` - falls back to the client's user cache.
    await sender._handle_raw_reaction(_reaction_payload(user_id=5, member=None), added=False)

    assert recorder.reactions == []


async def test_handle_raw_reaction_with_a_custom_emoji():
    recorder = _Recorder()
    client = FakeClient(user=FakeUser(id=1))
    sender = _make_sender(recorder, client)
    emoji = FakePartialEmoji(name="pepe", id=555, animated=True, url="https://cdn.example/pepe.png")

    await sender._handle_raw_reaction(_reaction_payload(emoji=emoji), added=True)

    [reaction] = recorder.reactions
    assert reaction.emoji.native_id == "555"
    assert reaction.emoji.name == "pepe"
    assert reaction.emoji.animated is True


async def test_handle_raw_reaction_carries_the_reactor_count():
    from tests.fakes.fake_discord import FakeChannel, FakeFullMessage, FakeReaction

    recorder = _Recorder()
    client = FakeClient(user=FakeUser(id=1))
    channel = client.add_channel(FakeChannel(id=42))
    channel.full_messages[7] = FakeFullMessage(7, reactions=[FakeReaction("\U0001f600", count=3, me=True)])
    sender = _make_sender(recorder, client)

    await sender._handle_raw_reaction(_reaction_payload(), added=True)

    assert recorder.reactions[0].origin_reactor_count == 3


async def test_handle_raw_reaction_reactor_count_is_none_when_the_fetch_fails():
    recorder = _Recorder()
    client = FakeClient(user=FakeUser(id=1))  # no channel registered -> fetch raises
    sender = _make_sender(recorder, client)

    await sender._handle_raw_reaction(_reaction_payload(), added=True)

    assert recorder.reactions[0].origin_reactor_count is None


# ---------------------------------------------------------------- _handle_guild_emojis_update


async def test_guild_emojis_update_ignores_a_different_guild():
    recorder = _Recorder()
    client = FakeClient()
    sender = _make_sender(recorder, client)

    await sender._handle_guild_emojis_update(FakeGuild(id=999), [], [SimpleNamespace(id=1)])

    assert recorder.emoji_created == []


async def test_guild_emojis_update_reports_a_newly_added_emoji():
    recorder = _Recorder()
    client = FakeClient()
    sender = _make_sender(recorder, client)
    new_emoji = SimpleNamespace(id=1, name="smile", url="https://cdn.example/e.png", animated=False, user=None)

    await sender._handle_guild_emojis_update(FakeGuild(id=123), [], [new_emoji])

    [created] = recorder.emoji_created
    assert created.emoji.native_id == "1"
    assert created.emoji.name == "smile"


async def test_guild_emojis_update_skips_an_emoji_mirrored_by_a_bot():
    recorder = _Recorder()
    client = FakeClient()
    sender = _make_sender(recorder, client)
    bot_user = SimpleNamespace(bot=True)
    new_emoji = SimpleNamespace(id=1, name="smile", url="https://cdn.example/e.png", animated=False, user=bot_user)

    await sender._handle_guild_emojis_update(FakeGuild(id=123), [], [new_emoji])

    assert recorder.emoji_created == []


async def test_guild_emojis_update_reports_a_removed_emoji():
    recorder = _Recorder()
    client = FakeClient()
    sender = _make_sender(recorder, client)
    old_emoji = SimpleNamespace(id=1, name="smile", url="https://cdn.example/e.png", animated=False, user=None)

    await sender._handle_guild_emojis_update(FakeGuild(id=123), [old_emoji], [])

    [deleted] = recorder.emoji_deleted
    assert deleted.native_id == "1"


async def test_guild_emojis_update_ignores_an_emoji_present_in_both_lists():
    recorder = _Recorder()
    client = FakeClient()
    sender = _make_sender(recorder, client)
    unchanged = SimpleNamespace(id=1, name="smile", url="https://cdn.example/e.png", animated=False, user=None)

    await sender._handle_guild_emojis_update(FakeGuild(id=123), [unchanged], [unchanged])

    assert recorder.emoji_created == []
    assert recorder.emoji_deleted == []


