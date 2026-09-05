from __future__ import annotations

import pytest

from stoat_discord_bridge.services.base import PartialRelayError
from stoat_discord_bridge.services.discord_service import DiscordReceiverService
from stoat_discord_bridge.storage.user_mappings import UserMapping, UserMappingRepository
from tests.fakes.fake_discord import FakeAsset, FakeChannel, FakeClient, FakeGuild, FakeUser
from tests.discord_receiver.conftest import _edit, _make_receiver, _message


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


async def test_edit_message_patches_the_relayed_webhook_post():
    client = FakeClient()
    channel = client.add_channel(FakeChannel(id=42))
    receiver = _make_receiver(client)

    await receiver.edit_message(target_channel_id="42", target_message_ids=["1000"], edit=_edit())

    webhook = channel.created_webhooks[0]
    assert [(e["message_id"], e["content"]) for e in webhook.edited] == [(1000, "edited text")]


async def test_edit_message_blanks_the_posts_a_shortened_edit_no_longer_fills():
    client = FakeClient()
    channel = client.add_channel(FakeChannel(id=42))
    receiver = _make_receiver(client)

    await receiver.edit_message(
        target_channel_id="42", target_message_ids=["1000", "1001"], edit=_edit(new_content_markdown="now short")
    )

    webhook = channel.created_webhooks[0]
    assert [(e["message_id"], e["content"]) for e in webhook.edited] == [(1000, "now short"), (1001, "​")]


async def test_edit_message_matches_each_split_chunk_to_its_post(monkeypatch):
    client = FakeClient()
    channel = client.add_channel(FakeChannel(id=42))
    receiver = _make_receiver(client)
    monkeypatch.setattr("stoat_discord_bridge.services.discord_service._CONTENT_LIMIT", 5)

    await receiver.edit_message(
        target_channel_id="42", target_message_ids=["1000", "1001"], edit=_edit(new_content_markdown="abcdefghij")
    )

    webhook = channel.created_webhooks[0]
    assert [e["content"] for e in webhook.edited] == ["abcde", "fghij"]


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


# -------------------------------------------------------------- source forwarding


async def test_receive_folds_the_source_label_into_the_webhook_username():
    client = FakeClient()
    channel = client.add_channel(FakeChannel(id=42))
    receiver = _make_receiver(client)

    await receiver.receive(_message(source_label="Stoat (public)"), target_channel_id="42")

    assert channel.created_webhooks[0].sent[0]["username"] == "Alice [Stoat (public)]"


async def test_receive_omits_the_source_label_when_source_forwarding_is_off():
    client = FakeClient()
    channel = client.add_channel(FakeChannel(id=42))
    receiver = DiscordReceiverService(client, guild_id=123, connector_id="discord", source_forwarding=False)

    await receiver.receive(_message(source_label="Stoat (public)"), target_channel_id="42")

    assert channel.created_webhooks[0].sent[0]["username"] == "Alice"


async def test_receive_folds_source_and_pronouns_into_the_webhook_username():
    client = FakeClient()
    channel = client.add_channel(FakeChannel(id=42))
    receiver = _make_receiver(client)

    await receiver.receive(
        _message(source_label="Stoat (public)", sender_pronouns="she/her"), target_channel_id="42"
    )

    assert channel.created_webhooks[0].sent[0]["username"] == "Alice [Stoat (public), she/her]"


async def test_receive_omits_pronouns_when_pronoun_forwarding_is_off():
    client = FakeClient()
    channel = client.add_channel(FakeChannel(id=42))
    receiver = DiscordReceiverService(
        client, guild_id=123, connector_id="discord", pronoun_forwarding=False
    )

    await receiver.receive(
        _message(source_label="IRC", sender_pronouns="she/her"), target_channel_id="42"
    )

    assert channel.created_webhooks[0].sent[0]["username"] == "Alice [IRC]"


async def test_receive_source_label_containing_discord_is_masked_by_the_username_sanitizer():
    # A second Discord connector's label is literally "Discord"; the webhook
    # API rejects any username containing "discord", so _sanitize_username
    # masks it rather than letting the send fail.
    client = FakeClient()
    channel = client.add_channel(FakeChannel(id=42))
    receiver = _make_receiver(client)

    await receiver.receive(_message(source_label="Discord"), target_channel_id="42")

    assert channel.created_webhooks[0].sent[0]["username"] == "Alice [*******]"


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


