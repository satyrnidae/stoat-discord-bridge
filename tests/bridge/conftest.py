"""Tests for BridgeCoordinator - the routing/fan-out logic that wires every
connector's sender output to every other connector's receiver.

Exercises it against real ChannelMappingRepository/MessageSyncRepository/
EmojiMappingRepository instances (backed by conftest.py's in-memory fake
Mongo, same as the storage-repository test suites) and hand-rolled
FakeReceiver stand-ins for the actual Discord/Stoat/IRC network services -
no live server needed, since BridgeCoordinator only ever talks to the
ReceiverService interface (services/base.py), never a platform client
directly.
"""

from __future__ import annotations

import pytest

from stoat_discord_bridge.bridge import BridgeCoordinator
from stoat_discord_bridge.models import CustomEmoji, StandardMessage
from stoat_discord_bridge.services.base import ReceiverService
from stoat_discord_bridge.status import HealthTracker
from stoat_discord_bridge.storage.channel_mappings import ChannelMapping, ChannelMappingRepository
from stoat_discord_bridge.storage.emoji_mappings import EmojiMappingRepository, EmojiRef
from stoat_discord_bridge.storage.message_sync import MessageRef, MessageSyncRepository


class FakeReceiver(ReceiverService):
    def __init__(
        self,
        connector_id: str,
        *,
        supports_reactions: bool = False,
        supports_emoji: bool = False,
        supports_pins: bool = False,
        supports_typing: bool = False,
        supports_edits: bool = False,
        native_ids: list[str] | None = None,
        raises: BaseException | None = None,
        created_emoji: CustomEmoji | None = None,
    ) -> None:
        self.connector_id = connector_id
        self.supports_reactions = supports_reactions
        self.supports_emoji = supports_emoji
        self.supports_pins = supports_pins
        self.supports_typing = supports_typing
        self.supports_edits = supports_edits
        self._native_ids = native_ids if native_ids is not None else ["native-1"]
        self._raises = raises
        self._created_emoji = created_emoji
        self.received: list[tuple] = []
        self.reactions: list[tuple] = []
        self.created_calls: list[CustomEmoji] = []
        self.pins: list[tuple] = []
        self.typing: list[str] = []
        self.typing_stopped: list[str] = []
        self.edits: list[tuple] = []

    async def receive(self, message: StandardMessage, *, target_channel_id: str) -> list[str]:
        self.received.append((message, target_channel_id))
        if self._raises is not None:
            raise self._raises
        return self._native_ids

    async def add_reaction(self, *, target_channel_id, target_message_id, emoji) -> None:
        if self._raises is not None:
            raise self._raises
        self.reactions.append(("add", target_channel_id, target_message_id, emoji))

    async def remove_reaction(self, *, target_channel_id, target_message_id, emoji) -> None:
        if self._raises is not None:
            raise self._raises
        self.reactions.append(("remove", target_channel_id, target_message_id, emoji))

    async def create_emoji(self, emoji: CustomEmoji) -> CustomEmoji | None:
        self.created_calls.append(emoji)
        if self._raises is not None:
            raise self._raises
        return self._created_emoji

    async def set_pinned(self, *, target_channel_id, target_message_id, pinned) -> None:
        if self._raises is not None:
            raise self._raises
        self.pins.append((target_channel_id, target_message_id, pinned))

    async def edit_message(self, *, target_channel_id, target_message_ids, edit) -> None:
        if self._raises is not None:
            raise self._raises
        self.edits.append((target_channel_id, tuple(target_message_ids), edit.new_content_markdown))

    async def trigger_typing(self, *, target_channel_id) -> None:
        if self._raises is not None:
            raise self._raises
        self.typing.append(target_channel_id)

    async def stop_typing(self, *, target_channel_id) -> None:
        if self._raises is not None:
            raise self._raises
        self.typing_stopped.append(target_channel_id)


def _message(**overrides) -> StandardMessage:
    defaults = dict(
        origin_connector_id="discord",
        origin_channel_id="100",
        channel_name="general",
        sender_name="Alice",
        sender_avatar_url=None,
        sender_user_id="alice-id",
        content_markdown="hi",
        message_id="m1",
    )
    defaults.update(overrides)
    return StandardMessage(**defaults)


@pytest.fixture
async def coordinator_parts(fake_db):
    channel_mappings = ChannelMappingRepository(fake_db)
    message_sync = MessageSyncRepository(fake_db)
    emoji_mappings = EmojiMappingRepository(fake_db)
    # mirrors bridge.py's run(): the unique (connector_id, emoji_id) index is
    # what makes try_reserve's duplicate-event guard race-proof, and isn't
    # created automatically by the repository itself.
    await emoji_mappings.ensure_indexes()
    health = HealthTracker({"discord": "Discord", "stoat": "Stoat", "irc": "IRC"})
    for connector_id in ("discord", "stoat", "irc"):
        health.mark_connected(connector_id)
    coordinator = BridgeCoordinator(channel_mappings, message_sync, emoji_mappings, health)
    return coordinator, channel_mappings, message_sync, emoji_mappings, health


async def _link(channel_mappings: ChannelMappingRepository, group: str, connector_id: str, channel_id: str) -> None:
    await channel_mappings.upsert(
        ChannelMapping(bridge_group=group, connector_id=connector_id, channel_id=channel_id, channel_name=channel_id)
    )


def _ref(connector_id: str, channel_id: str, message_id: str) -> MessageRef:
    return MessageRef(connector_id=connector_id, channel_id=channel_id, message_id=message_id)


def _emoji_ref(connector_id: str, emoji_id: str) -> EmojiRef:
    return EmojiRef(connector_id=connector_id, emoji_id=emoji_id, name="smile")
