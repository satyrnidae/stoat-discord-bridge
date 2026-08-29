"""Tests for DiscordReceiverService against the fake_discord scaffolding
(tests/fakes/fake_discord.py) - receive()'s webhook posting/chunking/
partial-failure behavior, reaction add/remove, custom emoji mirroring
(aiohttp's image download is monkeypatched, not real), and webhook
get-or-create/caching (migrated from the now-removed test_discord_webhook.py,
which used narrower ad hoc fakes for the same thing).
"""

from __future__ import annotations

from types import SimpleNamespace

import aiohttp
import pytest

from stoat_discord_bridge.models import CustomEmoji, StandardMessage
from stoat_discord_bridge.services.discord_service import DiscordReceiverService
from stoat_discord_bridge.services.base import PartialRelayError
from stoat_discord_bridge.storage.user_mappings import UserMapping, UserMappingRepository
from tests.fakes.fake_discord import (
    FakeAsset,
    FakeChannel,
    FakeClient,
    FakeFullMessage,
    FakeGuild,
    FakeReaction,
    FakeThread,
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
        sender_user_id="stoat-alice",
        content_markdown="hello",
        message_id="m1",
    )
    defaults.update(overrides)
    return StandardMessage(**defaults)


def _make_receiver(client: FakeClient, user_mappings: UserMappingRepository | None = None) -> DiscordReceiverService:
    return DiscordReceiverService(client, guild_id=123, connector_id="discord", user_mappings=user_mappings)


# ---------------------------------------------------------------- receive()


async def test_receive_posts_through_the_channels_webhook():
    client = FakeClient()
    channel = client.add_channel(FakeChannel(id=42))
    receiver = _make_receiver(client)

    ids = await receiver.receive(_message(), target_channel_id="42")

    webhook = channel.created_webhooks[0]
    assert webhook.sent == [
        {"content": "hello", "username": "Alice", "avatar_url": "https://cdn.example/alice.png", "thread": None}
    ]
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

    webhook, _thread = await receiver._get_or_create_webhook("42")
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


# ---------------------------------------------------------- linked-user masquerade


async def test_receive_masquerades_as_the_linked_local_user_when_linked(fake_db):
    user_mappings = UserMappingRepository(fake_db)
    await user_mappings.upsert(
        UserMapping(link_group="g1", connector_id="stoat", user_id="stoat-alice", display_name="stoat-alice")
    )
    await user_mappings.upsert(UserMapping(link_group="g1", connector_id="discord", user_id="42", display_name="42"))
    client = FakeClient()
    client.add_user(FakeUser(id=42, display_name="Local Alice", display_avatar=FakeAsset("https://cdn.example/local.png")))
    channel = client.add_channel(FakeChannel(id=99))
    receiver = _make_receiver(client, user_mappings)

    await receiver.receive(_message(), target_channel_id="99")

    webhook = channel.created_webhooks[0]
    assert webhook.sent == [
        {
            "content": "hello",
            "username": "Local Alice",
            "avatar_url": "https://cdn.example/local.png",
            "thread": None,
        }
    ]


async def test_receive_prefers_the_guild_members_nickname_over_the_global_username(fake_db):
    user_mappings = UserMappingRepository(fake_db)
    await user_mappings.upsert(
        UserMapping(link_group="g1", connector_id="stoat", user_id="stoat-alice", display_name="stoat-alice")
    )
    await user_mappings.upsert(UserMapping(link_group="g1", connector_id="discord", user_id="42", display_name="42"))
    client = FakeClient()
    # the global user (username) and the guild member (server nickname) differ.
    client.add_user(FakeUser(id=42, display_name="global-username"))
    guild = client.add_guild(FakeGuild(id=123))
    guild.add_member(FakeUser(id=42, display_name="Server Nickname", display_avatar=FakeAsset("https://cdn.example/nick.png")))
    channel = client.add_channel(FakeChannel(id=99))
    receiver = _make_receiver(client, user_mappings)

    await receiver.receive(_message(), target_channel_id="99")

    webhook = channel.created_webhooks[0]
    assert webhook.sent[0]["username"] == "Server Nickname"
    assert webhook.sent[0]["avatar_url"] == "https://cdn.example/nick.png"


async def test_receive_uses_the_remote_identity_when_the_sender_isnt_linked(fake_db):
    user_mappings = UserMappingRepository(fake_db)
    client = FakeClient()
    channel = client.add_channel(FakeChannel(id=99))
    receiver = _make_receiver(client, user_mappings)

    await receiver.receive(_message(), target_channel_id="99")

    webhook = channel.created_webhooks[0]
    assert webhook.sent[0]["username"] == "Alice"
    assert webhook.sent[0]["avatar_url"] == "https://cdn.example/alice.png"


async def test_receive_falls_back_to_the_remote_identity_when_the_linked_discord_user_cant_be_resolved(fake_db):
    user_mappings = UserMappingRepository(fake_db)
    await user_mappings.upsert(
        UserMapping(link_group="g1", connector_id="stoat", user_id="stoat-alice", display_name="stoat-alice")
    )
    await user_mappings.upsert(UserMapping(link_group="g1", connector_id="discord", user_id="42", display_name="42"))
    client = FakeClient()  # discord user 42 never added - get_user/fetch_user both miss
    channel = client.add_channel(FakeChannel(id=99))
    receiver = _make_receiver(client, user_mappings)

    await receiver.receive(_message(), target_channel_id="99")

    webhook = channel.created_webhooks[0]
    assert webhook.sent[0]["username"] == "Alice"
    assert webhook.sent[0]["avatar_url"] == "https://cdn.example/alice.png"


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
    client.add_guild(
        FakeGuild(id=123, raises=aiohttp.ClientResponseError(SimpleNamespace(real_url="https://cdn.example"), (), status=400))
    )
    receiver = _make_receiver(client)

    result = await receiver.create_emoji(CustomEmoji(native_id="src-1", name="smile", image_url="https://cdn.example/e.png"))

    assert result is None
    await receiver.close()


# ---------------------------------------------------------------- _get_or_create_webhook


async def test_creates_a_webhook_stamped_with_the_bots_avatar_when_none_exists():
    client = FakeClient()
    channel = client.add_channel(FakeChannel(id=42))
    receiver = _make_receiver(client)

    webhook, thread = await receiver._get_or_create_webhook("42")

    assert webhook.created_with == {"name": "Bridge", "avatar": b"avatar-bytes"}
    assert thread is None


async def test_reuses_an_existing_bridge_webhook_without_touching_its_avatar():
    client = FakeClient()
    existing = FakeWebhook(id=99, user=client.user)
    channel = client.add_channel(FakeChannel(id=42, webhooks=[existing]))
    receiver = _make_receiver(client)

    webhook, _thread = await receiver._get_or_create_webhook("42")

    assert webhook is existing
    assert channel.created_webhooks == []


async def test_ignores_a_webhook_owned_by_a_different_user():
    client = FakeClient()
    other_webhook = FakeWebhook(id=1, user=FakeUser(id=555))
    channel = client.add_channel(FakeChannel(id=42, webhooks=[other_webhook]))
    receiver = _make_receiver(client)

    webhook, _thread = await receiver._get_or_create_webhook("42")

    assert webhook is not other_webhook
    assert len(channel.created_webhooks) == 1


async def test_caches_the_webhook_across_calls():
    client = FakeClient()
    channel = client.add_channel(FakeChannel(id=42))
    receiver = _make_receiver(client)

    first, _thread1 = await receiver._get_or_create_webhook("42")
    second, _thread2 = await receiver._get_or_create_webhook("42")

    assert first is second
    assert len(channel.created_webhooks) == 1


async def test_resolves_the_parent_channels_webhook_for_a_thread():
    client = FakeClient()
    parent = client.add_channel(FakeChannel(id=42))
    thread = client.add_channel(FakeThread(id=777, parent=parent))
    receiver = _make_receiver(client)

    webhook, resolved_thread = await receiver._get_or_create_webhook("777")

    assert webhook.created_with == {"name": "Bridge", "avatar": b"avatar-bytes"}
    assert parent.created_webhooks == [webhook]
    assert resolved_thread is thread


async def test_threads_under_the_same_parent_share_one_cached_webhook():
    client = FakeClient()
    parent = client.add_channel(FakeChannel(id=42))
    thread_a = client.add_channel(FakeThread(id=777, parent=parent))
    thread_b = client.add_channel(FakeThread(id=778, parent=parent))
    receiver = _make_receiver(client)

    webhook_a, _ = await receiver._get_or_create_webhook("777")
    webhook_b, _ = await receiver._get_or_create_webhook("778")

    assert webhook_a is webhook_b
    assert len(parent.created_webhooks) == 1


async def test_receive_posts_into_a_thread_through_its_parents_webhook():
    client = FakeClient()
    parent = client.add_channel(FakeChannel(id=42))
    thread = client.add_channel(FakeThread(id=777, parent=parent))
    receiver = _make_receiver(client)

    ids = await receiver.receive(_message(), target_channel_id="777")

    webhook = parent.created_webhooks[0]
    assert webhook.sent == [
        {"content": "hello", "username": "Alice", "avatar_url": "https://cdn.example/alice.png", "thread": thread}
    ]
    assert ids == ["1000"]


# ---------------------------------------------------------------- set_pinned


async def test_set_pinned_pins_and_unpins_the_message():
    client = FakeClient()
    channel = client.add_channel(FakeChannel(id=42))
    receiver = _make_receiver(client)

    await receiver.set_pinned(target_channel_id="42", target_message_id="7", pinned=True)
    msg = await channel.fetch_message(7)
    assert msg.pinned is True
    assert msg.pin_calls == ["bridge pin sync"]

    await receiver.set_pinned(target_channel_id="42", target_message_id="7", pinned=False)
    assert msg.pinned is False
    assert msg.unpin_calls == ["bridge pin sync"]


async def test_set_pinned_is_a_noop_when_already_in_the_target_state():
    client = FakeClient()
    channel = client.add_channel(FakeChannel(id=42))
    channel.full_messages[7] = pinned_msg = FakeFullMessage(id=7, pinned=True)
    receiver = _make_receiver(client)

    await receiver.set_pinned(target_channel_id="42", target_message_id="7", pinned=True)

    assert pinned_msg.pin_calls == []
