"""Tests for DiscordReceiverService against the fake_discord scaffolding
(tests/fakes/fake_discord.py) - receive()'s webhook posting/chunking/
partial-failure behavior, reaction add/remove, custom emoji mirroring
(aiohttp's image download is monkeypatched, not real), and webhook
get-or-create/caching (migrated from the now-removed test_discord_webhook.py,
which used narrower ad hoc fakes for the same thing).
"""

from __future__ import annotations

import aiohttp
import pytest

from stoat_discord_bridge.models import CustomEmoji, StandardMessage
from stoat_discord_bridge.services.discord_service import DiscordReceiverService
from stoat_discord_bridge.services.base import PartialRelayError
from tests.fakes.fake_discord import (
    FakeChannel,
    FakeClient,
    FakeGuild,
    FakeUser,
    FakeWebhook,
)


def _message(**overrides) -> StandardMessage:
    defaults = dict(
        origin_connector_id="stoat",
        origin_channel_id="s-100",
        channel_name="general",
        sender_name="Alice",
        sender_avatar_url="https://cdn.example/alice.png",
        content_markdown="hello",
        message_id="m1",
    )
    defaults.update(overrides)
    return StandardMessage(**defaults)


def _make_receiver(client: FakeClient) -> DiscordReceiverService:
    return DiscordReceiverService(client, guild_id=123, connector_id="discord")


# ---------------------------------------------------------------- receive()


async def test_receive_posts_through_the_channels_webhook():
    client = FakeClient()
    channel = client.add_channel(FakeChannel(id=42))
    receiver = _make_receiver(client)

    ids = await receiver.receive(_message(), target_channel_id="42")

    webhook = channel.created_webhooks[0]
    assert webhook.sent == [{"content": "hello", "username": "Alice", "avatar_url": "https://cdn.example/alice.png"}]
    assert ids == ["1000"]


async def test_receive_splits_long_content_into_multiple_webhook_sends(monkeypatch):
    client = FakeClient()
    channel = client.add_channel(FakeChannel(id=42))
    receiver = _make_receiver(client)
    monkeypatch.setattr("stoat_discord_bridge.services.discord_service._CONTENT_LIMIT", 5)

    ids = await receiver.receive(_message(content_markdown="abcdefghij"), target_channel_id="42")

    webhook = channel.created_webhooks[0]
    assert [call["content"] for call in webhook.sent] == ["abcde", "fghij"]
    assert len(ids) == 2


async def test_receive_raises_partial_relay_error_and_keeps_ids_already_sent(monkeypatch):
    client = FakeClient()
    channel = client.add_channel(FakeChannel(id=42))
    receiver = _make_receiver(client)
    monkeypatch.setattr("stoat_discord_bridge.services.discord_service._CONTENT_LIMIT", 5)

    webhook = await receiver._get_or_create_webhook("42")
    call_count = 0
    real_send = webhook.send

    async def flaky_send(**kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 2:
            raise RuntimeError("rate limited")
        return await real_send(**kwargs)

    webhook.send = flaky_send

    with pytest.raises(PartialRelayError) as exc_info:
        await receiver.receive(_message(content_markdown="abcdefghij"), target_channel_id="42")

    assert len(exc_info.value.partial_ids) == 1


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
    receiver = _make_receiver(client)

    await receiver.remove_reaction(target_channel_id="42", target_message_id="7", emoji="\U0001f600")

    removed = channel.get_partial_message(7).removed_reactions
    assert removed == [("\U0001f600", bot_user)]


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


class _FakeAiohttpResponse:
    def __init__(self, body: bytes, *, status: int = 200) -> None:
        self._body = body
        self.status = status

    async def __aenter__(self) -> "_FakeAiohttpResponse":
        return self

    async def __aexit__(self, *exc_info) -> bool:
        return False

    def raise_for_status(self) -> None:
        if self.status >= 400:
            raise aiohttp.ClientResponseError(None, (), status=self.status)

    async def read(self) -> bytes:
        return self._body


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
    client.add_guild(FakeGuild(id=123, raises=aiohttp.ClientResponseError(None, (), status=400)))
    receiver = _make_receiver(client)

    result = await receiver.create_emoji(CustomEmoji(native_id="src-1", name="smile", image_url="https://cdn.example/e.png"))

    assert result is None
    await receiver.close()


# ---------------------------------------------------------------- _get_or_create_webhook


async def test_creates_a_webhook_stamped_with_the_bots_avatar_when_none_exists():
    client = FakeClient()
    channel = client.add_channel(FakeChannel(id=42))
    receiver = _make_receiver(client)

    webhook = await receiver._get_or_create_webhook("42")

    assert webhook.created_with == {"name": "Bridge", "avatar": b"avatar-bytes"}


async def test_reuses_an_existing_bridge_webhook_without_touching_its_avatar():
    client = FakeClient()
    existing = FakeWebhook(id=99, user=client.user)
    channel = client.add_channel(FakeChannel(id=42, webhooks=[existing]))
    receiver = _make_receiver(client)

    webhook = await receiver._get_or_create_webhook("42")

    assert webhook is existing
    assert channel.created_webhooks == []


async def test_ignores_a_webhook_owned_by_a_different_user():
    client = FakeClient()
    other_webhook = FakeWebhook(id=1, user=FakeUser(id=555))
    channel = client.add_channel(FakeChannel(id=42, webhooks=[other_webhook]))
    receiver = _make_receiver(client)

    webhook = await receiver._get_or_create_webhook("42")

    assert webhook is not other_webhook
    assert len(channel.created_webhooks) == 1


async def test_caches_the_webhook_across_calls():
    client = FakeClient()
    channel = client.add_channel(FakeChannel(id=42))
    receiver = _make_receiver(client)

    first = await receiver._get_or_create_webhook("42")
    second = await receiver._get_or_create_webhook("42")

    assert first is second
    assert len(channel.created_webhooks) == 1
