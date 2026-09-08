from __future__ import annotations

from types import SimpleNamespace

import aiohttp

from stoat_discord_bridge.models import CustomEmoji
from tests.fakes.fake_stoat import FakeChannel, FakeClient, FakeServer
from tests.stoat_receiver.conftest import _FakeAiohttpResponse, _make_receiver


# ---------------------------------------------------------------- reactions


async def test_add_reaction_targets_the_right_message():
    client = FakeClient()
    channel = client.add_channel(FakeChannel(id="42"))
    receiver = _make_receiver(client)

    await receiver.add_reaction(target_channel_id="42", target_message_id="7", emoji="\U0001f600")

    assert channel.get_message("7").added_reactions == ["\U0001f600"]


async def test_remove_reaction_targets_the_right_message():
    client = FakeClient()
    channel = client.add_channel(FakeChannel(id="42"))
    channel.get_message("7").reactions["\U0001f600"] = ("bridge-bot-id",)
    receiver = _make_receiver(client)

    await receiver.remove_reaction(target_channel_id="42", target_message_id="7", emoji="\U0001f600")

    assert channel.get_message("7").removed_reactions == ["\U0001f600"]


async def test_add_reaction_skips_when_the_bot_already_reacted():
    client = FakeClient()
    channel = client.add_channel(FakeChannel(id="42"))
    channel.get_message("7").reactions["\U0001f600"] = ("someone", "bridge-bot-id")
    receiver = _make_receiver(client)

    await receiver.add_reaction(target_channel_id="42", target_message_id="7", emoji="\U0001f600")

    assert channel.get_message("7").added_reactions == []


async def test_remove_reaction_skips_when_the_bot_isnt_reacting():
    client = FakeClient()
    channel = client.add_channel(FakeChannel(id="42"))
    channel.get_message("7").reactions["\U0001f600"] = ("someone-else",)
    receiver = _make_receiver(client)

    await receiver.remove_reaction(target_channel_id="42", target_message_id="7", emoji="\U0001f600")

    assert channel.get_message("7").removed_reactions == []


async def test_add_reaction_translates_a_custom_emoji_to_its_native_id():
    client = FakeClient()
    channel = client.add_channel(FakeChannel(id="42"))
    receiver = _make_receiver(client)

    await receiver.add_reaction(
        target_channel_id="42",
        target_message_id="7",
        emoji=CustomEmoji(native_id="stoat-555", name="smile", image_url="https://cdn.example/e.png"),
    )

    assert channel.get_message("7").added_reactions == ["stoat-555"]


# ---------------------------------------------------------------- create_emoji


async def test_create_emoji_downloads_and_mirrors_it(monkeypatch):
    monkeypatch.setattr(aiohttp.ClientSession, "get", lambda self, url: _FakeAiohttpResponse(b"image-bytes"))
    client = FakeClient()
    server = client.add_server(FakeServer(id="srv-1"))
    receiver = _make_receiver(client)

    result = await receiver.create_emoji(CustomEmoji(native_id="e1", name="smile", image_url="https://cdn.example/e.png"))

    assert server.created_emoji_calls == [{"name": "smile", "image": b"image-bytes"}]
    assert result is not None
    assert result.name == "smile"


async def test_create_emoji_sanitises_the_name_to_stoats_charset(monkeypatch):
    monkeypatch.setattr(aiohttp.ClientSession, "get", lambda self, url: _FakeAiohttpResponse(b"image-bytes"))
    client = FakeClient()
    server = client.add_server(FakeServer(id="srv-1"))
    receiver = _make_receiver(client)

    await receiver.create_emoji(
        CustomEmoji(native_id="e1", name="Big Smile!", image_url="https://cdn.example/e.png")
    )

    assert server.created_emoji_calls == [{"name": "big_smile", "image": b"image-bytes"}]


async def test_create_emoji_returns_none_on_http_failure(monkeypatch):
    monkeypatch.setattr(aiohttp.ClientSession, "get", lambda self, url: _FakeAiohttpResponse(b"image-bytes"))
    client = FakeClient()
    client.add_server(
        FakeServer(id="srv-1", raises=aiohttp.ClientResponseError(SimpleNamespace(real_url="https://cdn.example"), (), status=400))
    )
    receiver = _make_receiver(client)

    result = await receiver.create_emoji(CustomEmoji(native_id="e1", name="smile", image_url="https://cdn.example/e.png"))

    assert result is None


