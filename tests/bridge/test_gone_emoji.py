"""A linked emoji copy deleted on its platform (a delete event the bridge
missed) is forgotten the next time a reaction or rename needs it, rather
than retried forever (issue #217)."""

from __future__ import annotations

import pytest

from stoat_discord_bridge.admin_commands import ConnectorInfo
from stoat_discord_bridge.bridge import BridgeCoordinator
from stoat_discord_bridge.models import CustomEmoji, StandardReaction
from tests.bridge.conftest import FakeReceiver, _link, _ref
from tests.bridge.test_emoji_rename import _link_emoji


@pytest.fixture
async def parts(coordinator_parts):
    _coordinator, channel_mappings, message_sync, emoji_mappings, health = coordinator_parts
    gone: set[tuple[str, str]] = set()

    async def entity_exists(kind: str, entity_id: str):
        return (kind, entity_id) not in gone

    connectors = {
        "discord": ConnectorInfo(id="discord", label="Discord"),
        "stoat": ConnectorInfo(id="stoat", label="Stoat", entity_exists=entity_exists),
    }
    coordinator = BridgeCoordinator(channel_mappings, message_sync, emoji_mappings, health, connectors)
    await _link(channel_mappings, "general", "discord", "100")
    await _link(channel_mappings, "general", "stoat", "200")
    await message_sync.record("general", _ref("discord", "100", "m1"), [_ref("stoat", "200", "s1")])
    await _link_emoji(emoji_mappings, ("discord", "d1"), ("stoat", "e1"), ("discord2", "d2"))
    return coordinator, emoji_mappings, gone


def _reaction() -> StandardReaction:
    return StandardReaction(
        origin_connector_id="discord",
        origin_channel_id="100",
        origin_message_id="m1",
        emoji=CustomEmoji(native_id="d1", name="pog", image_url="https://cdn.example/d1.png"),
        added=True,
    )


async def test_reaction_with_a_deleted_copy_is_skipped_and_the_copy_forgotten(parts):
    coordinator, emoji_mappings, gone = parts
    gone.add(("emoji", "e1"))
    stoat = FakeReceiver("stoat", supports_reactions=True)
    coordinator.register_receiver(stoat)

    await coordinator.handle_reaction(_reaction())

    assert stoat.reactions == []
    assert await emoji_mappings.get_group_id("stoat", "e1") is None
    assert await emoji_mappings.get_group_id("discord", "d1") is not None


async def test_reaction_with_a_live_copy_still_relays(parts):
    coordinator, emoji_mappings, _gone = parts
    stoat = FakeReceiver("stoat", supports_reactions=True)
    coordinator.register_receiver(stoat)

    await coordinator.handle_reaction(_reaction())

    assert len(stoat.reactions) == 1
    assert await emoji_mappings.get_group_id("stoat", "e1") is not None


async def test_rename_skips_and_forgets_a_deleted_copy(parts):
    coordinator, emoji_mappings, gone = parts
    stoat = FakeReceiver("stoat", supports_emoji_rename=True)
    coordinator.register_receiver(stoat)
    gone.add(("emoji", "e1"))

    await coordinator.handle_emoji_renamed("discord", "d1", "poggers")

    assert stoat.emoji_renames == []
    assert await emoji_mappings.get_group_id("stoat", "e1") is None
    assert await emoji_mappings.find_name("discord", "d1") == "poggers"
