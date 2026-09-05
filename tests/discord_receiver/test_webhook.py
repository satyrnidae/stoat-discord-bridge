from __future__ import annotations

import pytest

from stoat_discord_bridge.services.base import UnsupportedRelayTargetError
from tests.fakes.fake_discord import FakeChannel, FakeClient, FakeForumChannel, FakeThread, FakeUser, FakeWebhook
from tests.discord_receiver.conftest import _edit, _make_receiver, _message


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


async def test_get_or_create_webhook_rejects_a_forum_channel():
    client = FakeClient()
    forum = client.add_channel(FakeForumChannel(id=42))
    receiver = _make_receiver(client)

    with pytest.raises(UnsupportedRelayTargetError):
        await receiver._get_or_create_webhook("42")

    assert forum.created_webhooks == []


async def test_receive_rejects_a_forum_channel_target():
    client = FakeClient()
    client.add_channel(FakeForumChannel(id=42))
    receiver = _make_receiver(client)

    with pytest.raises(UnsupportedRelayTargetError):
        await receiver.receive(_message(), target_channel_id="42")


async def test_edit_message_rejects_a_forum_channel_target():
    client = FakeClient()
    client.add_channel(FakeForumChannel(id=42))
    receiver = _make_receiver(client)

    with pytest.raises(UnsupportedRelayTargetError):
        await receiver.edit_message(target_channel_id="42", target_message_ids=["1000"], edit=_edit())


async def test_receive_still_posts_into_a_forum_post_thread():
    client = FakeClient()
    forum = client.add_channel(FakeForumChannel(id=42))
    thread = client.add_channel(FakeThread(id=777, parent=forum))
    receiver = _make_receiver(client)

    ids = await receiver.receive(_message(), target_channel_id="777")

    webhook = forum.created_webhooks[0]
    assert webhook.sent[0]["thread"] is thread
    assert ids == ["1000"]


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


