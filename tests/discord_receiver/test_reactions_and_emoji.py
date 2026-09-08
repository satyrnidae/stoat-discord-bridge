from __future__ import annotations

from types import SimpleNamespace

import aiohttp

from stoat_discord_bridge.models import CustomEmoji
from tests.fakes.fake_discord import FakeChannel, FakeClient, FakeFullMessage, FakeGuild, FakeReaction, FakeUser
from tests.discord_receiver.conftest import _FakeAiohttpResponse, _make_receiver


# ---------------------------------------------------------------- reactions


async def test_add_reaction_targets_the_right_partial_message():
    client = FakeClient()
    channel = client.add_channel(FakeChannel(id=42))
    receiver = _make_receiver(client)

    await receiver.add_reaction(target_channel_id="42", target_message_id="7", emoji="\U0001f600")

    assert channel.get_partial_message(7).added_reactions == ["\U0001f600"]


async def test_remove_reaction_uses_the_bots_own_identity():
    bot_user = FakeUser(id=99)
    client = FakeClient(user=bot_user)
    channel = client.add_channel(FakeChannel(id=42))
    channel.full_messages[7] = FakeFullMessage(7, reactions=[FakeReaction("\U0001f600", me=True)])
    receiver = _make_receiver(client)

    await receiver.remove_reaction(target_channel_id="42", target_message_id="7", emoji="\U0001f600")

    removed = channel.get_partial_message(7).removed_reactions
    assert removed == [("\U0001f600", bot_user)]


async def test_add_reaction_skips_when_the_bot_already_reacted():
    client = FakeClient()
    channel = client.add_channel(FakeChannel(id=42))
    channel.full_messages[7] = FakeFullMessage(7, reactions=[FakeReaction("\U0001f600", count=2, me=True)])
    receiver = _make_receiver(client)

    await receiver.add_reaction(target_channel_id="42", target_message_id="7", emoji="\U0001f600")

    assert channel.get_partial_message(7).added_reactions == []


async def test_remove_reaction_skips_when_the_bot_isnt_reacting():
    client = FakeClient()
    channel = client.add_channel(FakeChannel(id=42))
    channel.full_messages[7] = FakeFullMessage(7, reactions=[FakeReaction("\U0001f600", count=1, me=False)])
    receiver = _make_receiver(client)

    await receiver.remove_reaction(target_channel_id="42", target_message_id="7", emoji="\U0001f600")

    assert channel.get_partial_message(7).removed_reactions == []


async def test_add_reaction_translates_a_custom_emoji():
    client = FakeClient()
    channel = client.add_channel(FakeChannel(id=42))
    receiver = _make_receiver(client)

    await receiver.add_reaction(
        target_channel_id="42",
        target_message_id="7",
        emoji=CustomEmoji(native_id="555", name="smile", image_url="https://cdn.example/e.png"),
    )

    [emoji] = channel.get_partial_message(7).added_reactions
    assert emoji.id == 555
    assert emoji.name == "smile"


# ---------------------------------------------------------------- create_emoji


async def test_create_emoji_downloads_and_mirrors_it(monkeypatch):
    monkeypatch.setattr(aiohttp.ClientSession, "get", lambda self, url: _FakeAiohttpResponse(b"image-bytes"))
    client = FakeClient()
    guild = client.add_guild(FakeGuild(id=123))
    receiver = _make_receiver(client)

    result = await receiver.create_emoji(
        CustomEmoji(native_id="src-1", name="my emoji!!", image_url="https://cdn.example/e.png")
    )

    assert guild.created_emoji_calls == [{"name": "my_emoji", "image": b"image-bytes"}]
    assert result is not None
    assert result.name == "my_emoji"
    await receiver.close()


async def test_create_emoji_returns_none_when_the_guild_isnt_cached():
    client = FakeClient()  # no guild added
    receiver = _make_receiver(client)

    result = await receiver.create_emoji(CustomEmoji(native_id="src-1", name="smile", image_url="https://cdn.example/e.png"))

    assert result is None


async def test_create_emoji_returns_none_when_discord_rejects_it(monkeypatch):
    monkeypatch.setattr(aiohttp.ClientSession, "get", lambda self, url: _FakeAiohttpResponse(b"image-bytes"))
    client = FakeClient()
    client.add_guild(
        FakeGuild(id=123, raises=aiohttp.ClientResponseError(SimpleNamespace(real_url="https://cdn.example"), (), status=400))
    )
    receiver = _make_receiver(client)

    result = await receiver.create_emoji(CustomEmoji(native_id="src-1", name="smile", image_url="https://cdn.example/e.png"))

    assert result is None
    await receiver.close()


