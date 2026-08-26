"""Tests for DiscordReceiverService._get_or_create_webhook. Creating a new
webhook stamps it with the bot's own avatar, so a relayed message whose
sender avatar couldn't be resolved (see _resolve_avatar_url in
stoat_service.py) falls back to looking like it came from the bridge bot
rather than Discord's blank/generic default. An already-existing webhook is
reused as-is - its avatar might have been customized by an admin, so it's
never overwritten here.
"""

from __future__ import annotations

from types import SimpleNamespace

from stoat_discord_bridge.services.discord_service import DiscordReceiverService


class FakeAvatarAsset:
    async def read(self) -> bytes:
        return b"bot-avatar-bytes"


def _make_receiver(client) -> DiscordReceiverService:
    return DiscordReceiverService(client, guild_id=123, connector_id="discord")


def _fake_client(channel):
    bot_user = SimpleNamespace(display_avatar=FakeAvatarAsset())
    return SimpleNamespace(user=bot_user, get_channel=lambda _id: channel), bot_user


async def test_creates_a_webhook_stamped_with_the_bots_avatar_when_none_exists():
    created = []

    async def fake_webhooks():
        return []

    async def fake_create_webhook(*, name, avatar=None):
        created.append({"name": name, "avatar": avatar})
        return SimpleNamespace(id=1)

    channel = SimpleNamespace(webhooks=fake_webhooks, create_webhook=fake_create_webhook)
    client, _bot_user = _fake_client(channel)

    receiver = _make_receiver(client)
    webhook = await receiver._get_or_create_webhook("42")

    assert created == [{"name": "Bridge", "avatar": b"bot-avatar-bytes"}]
    assert webhook.id == 1


async def test_reuses_an_existing_bridge_webhook_without_touching_its_avatar():
    async def fake_create_webhook(**kwargs):
        raise AssertionError("should not create a new webhook when one already exists")

    channel = SimpleNamespace(create_webhook=fake_create_webhook)
    client, bot_user = _fake_client(channel)
    existing = SimpleNamespace(id=99, user=bot_user)

    async def fake_webhooks():
        return [existing]

    channel.webhooks = fake_webhooks

    receiver = _make_receiver(client)
    webhook = await receiver._get_or_create_webhook("42")

    assert webhook is existing


async def test_ignores_a_webhook_owned_by_a_different_user():
    async def fake_create_webhook(*, name, avatar=None):
        return SimpleNamespace(id=1, user="me")

    other_users_webhook = SimpleNamespace(id=1, user=SimpleNamespace())

    async def fake_webhooks():
        return [other_users_webhook]

    channel = SimpleNamespace(webhooks=fake_webhooks, create_webhook=fake_create_webhook)
    client, _bot_user = _fake_client(channel)

    receiver = _make_receiver(client)
    webhook = await receiver._get_or_create_webhook("42")

    assert webhook is not other_users_webhook


async def test_caches_the_webhook_across_calls():
    calls = 0

    async def fake_webhooks():
        return []

    async def fake_create_webhook(*, name, avatar=None):
        nonlocal calls
        calls += 1
        return SimpleNamespace(id=calls)

    channel = SimpleNamespace(webhooks=fake_webhooks, create_webhook=fake_create_webhook)
    client, _bot_user = _fake_client(channel)

    receiver = _make_receiver(client)
    first = await receiver._get_or_create_webhook("42")
    second = await receiver._get_or_create_webhook("42")

    assert first is second
    assert calls == 1
