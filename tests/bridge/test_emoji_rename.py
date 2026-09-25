"""Tests for BridgeCoordinator.handle_emoji_renamed (issue #175) - the emoji
counterpart of handle_channel_renamed."""

from __future__ import annotations

from stoat_discord_bridge.storage.emoji_mappings import EmojiMappingRepository, EmojiRef
from tests.bridge.conftest import FakeReceiver


async def _link_emoji(emoji_mappings: EmojiMappingRepository, *refs: tuple[str, str]) -> None:
    (first_connector, first_id), *rest = refs
    group_id = await emoji_mappings.try_reserve(EmojiRef(connector_id=first_connector, emoji_id=first_id, name="pog"))
    await emoji_mappings.add_refs(group_id, [EmojiRef(connector_id=c, emoji_id=e, name="pog") for c, e in rest])


async def test_emoji_renamed_renames_supporting_copies_and_refreshes_names(coordinator_parts):
    coordinator, _channels, _sync, emoji_mappings, _health = coordinator_parts
    await _link_emoji(emoji_mappings, ("discord", "d1"), ("discord2", "d2"), ("stoat", "s1"))
    other_discord = FakeReceiver("discord2", supports_emoji_rename=True)
    stoat = FakeReceiver("stoat", supports_emoji_rename=False)
    coordinator.register_receiver(other_discord)
    coordinator.register_receiver(stoat)

    await coordinator.handle_emoji_renamed("discord", "d1", "poggers")

    assert other_discord.emoji_renames == [("d2", "poggers")]
    assert stoat.emoji_renames == []
    assert await emoji_mappings.find_name("discord", "d1") == "poggers"
    assert await emoji_mappings.find_name("discord2", "d2") == "poggers"
    # Stoat can't rename in place, so its stored name still matches its real emoji
    assert await emoji_mappings.find_name("stoat", "s1") == "pog"


async def test_emoji_renamed_is_a_noop_for_an_unlinked_emoji(coordinator_parts):
    coordinator, _channels, _sync, emoji_mappings, _health = coordinator_parts
    receiver = FakeReceiver("discord2", supports_emoji_rename=True)
    coordinator.register_receiver(receiver)

    await coordinator.handle_emoji_renamed("discord", "d1", "poggers")  # must not raise

    assert receiver.emoji_renames == []


async def test_emoji_renamed_leaves_the_stored_name_when_the_rename_fails(coordinator_parts):
    coordinator, _channels, _sync, emoji_mappings, _health = coordinator_parts
    await _link_emoji(emoji_mappings, ("discord", "d1"), ("discord2", "d2"))
    coordinator.register_receiver(FakeReceiver("discord2", supports_emoji_rename=True, rename_fails=True))

    await coordinator.handle_emoji_renamed("discord", "d1", "poggers")

    assert await emoji_mappings.find_name("discord", "d1") == "poggers"
    assert await emoji_mappings.find_name("discord2", "d2") == "pog"


async def test_emoji_renamed_swallows_a_raising_receiver(coordinator_parts):
    coordinator, _channels, _sync, emoji_mappings, _health = coordinator_parts
    await _link_emoji(emoji_mappings, ("discord", "d1"), ("discord2", "d2"))
    coordinator.register_receiver(FakeReceiver("discord2", supports_emoji_rename=True, raises=RuntimeError("boom")))

    await coordinator.handle_emoji_renamed("discord", "d1", "poggers")  # must not raise

    assert await emoji_mappings.find_name("discord2", "d2") == "pog"


async def test_emoji_renamed_drops_the_echo_of_its_own_rename(coordinator_parts):
    coordinator, _channels, _sync, emoji_mappings, _health = coordinator_parts
    await _link_emoji(emoji_mappings, ("discord", "d1"), ("discord2", "d2"))
    origin = FakeReceiver("discord", supports_emoji_rename=True)
    other = FakeReceiver("discord2", supports_emoji_rename=True)
    coordinator.register_receiver(origin)
    coordinator.register_receiver(other)

    await coordinator.handle_emoji_renamed("discord", "d1", "poggers")
    # discord2's gateway now reports the rename the bridge just applied there
    await coordinator.handle_emoji_renamed("discord2", "d2", "poggers")

    assert origin.emoji_renames == []
    assert other.emoji_renames == [("d2", "poggers")]
