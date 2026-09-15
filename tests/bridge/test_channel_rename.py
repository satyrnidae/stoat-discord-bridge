"""Tests for BridgeCoordinator.handle_channel_renamed (issue #152) - the
channel-mapping-keyed counterpart of RoleSyncCoordinator.handle_role_renamed,
but living on BridgeCoordinator itself and driving ReceiverService.rename_channel
rather than a ConnectorInfo hook (ConnectorInfo is the admin-command linkers'
surface, unrelated to this live-sync path)."""

from __future__ import annotations

from tests.bridge.conftest import FakeReceiver, _link


async def test_channel_renamed_renames_every_other_mapped_channel(coordinator_parts):
    coordinator, channel_mappings, _message_sync, _emoji_mappings, _health = coordinator_parts
    await _link(channel_mappings, "general", "discord", "100")
    await _link(channel_mappings, "general", "stoat", "200")
    await _link(channel_mappings, "general", "irc", "300")
    stoat_receiver = FakeReceiver("stoat", supports_channel_rename=True)
    irc_receiver = FakeReceiver("irc", supports_channel_rename=False)
    coordinator.register_receiver(stoat_receiver)
    coordinator.register_receiver(irc_receiver)

    await coordinator.handle_channel_renamed("discord", "100", "new-name")

    assert stoat_receiver.renames == [("200", "new-name")]
    assert irc_receiver.renames == []


async def test_channel_renamed_refreshes_the_stored_channel_name(coordinator_parts):
    coordinator, channel_mappings, _message_sync, _emoji_mappings, _health = coordinator_parts
    await _link(channel_mappings, "general", "discord", "100")
    await _link(channel_mappings, "general", "stoat", "200")
    coordinator.register_receiver(FakeReceiver("stoat", supports_channel_rename=True))

    await coordinator.handle_channel_renamed("discord", "100", "new-name")

    mapped = await channel_mappings.get_mapped_channels("general")
    names = {m.connector_id: m.channel_name for m in mapped}
    assert names == {"discord": "new-name", "stoat": "new-name"}


async def test_channel_renamed_stores_the_name_the_receiver_actually_applied(coordinator_parts):
    # A target's own name-length limit can force rename_channel to apply a
    # clipped/truncated name - the stored mapping must reflect that, not the
    # raw requested new_name, or the next identical rename would be wrongly
    # skipped as already-applied while the DB drifts from the real name.
    coordinator, channel_mappings, _message_sync, _emoji_mappings, _health = coordinator_parts
    await _link(channel_mappings, "general", "discord", "100")
    await _link(channel_mappings, "general", "stoat", "200")
    stoat_receiver = FakeReceiver("stoat", supports_channel_rename=True, channel_name_limit=5)
    coordinator.register_receiver(stoat_receiver)

    await coordinator.handle_channel_renamed("discord", "100", "a-very-long-new-name")

    assert stoat_receiver.renames == [("200", "a-ver")]
    mapped = await channel_mappings.get_mapped_channels("general")
    names = {m.connector_id: m.channel_name for m in mapped}
    assert names == {"discord": "a-very-long-new-name", "stoat": "a-ver"}


async def test_channel_renamed_leaves_the_stored_name_untouched_when_the_receiver_reports_failure(
    coordinator_parts,
):
    # rename_channel signals a failure it handled internally (no exception)
    # by returning None - the stored name and suppress bookkeeping must both
    # treat that exactly like a raised exception, not like success.
    coordinator, channel_mappings, _message_sync, _emoji_mappings, _health = coordinator_parts
    await _link(channel_mappings, "general", "discord", "100")
    await _link(channel_mappings, "general", "stoat", "200")
    stoat_receiver = FakeReceiver("stoat", supports_channel_rename=True, rename_fails=True)
    coordinator.register_receiver(stoat_receiver)

    await coordinator.handle_channel_renamed("discord", "100", "new-name")  # must not raise

    assert stoat_receiver.renames == []
    mapped = await channel_mappings.get_mapped_channels("general")
    names = {m.connector_id: m.channel_name for m in mapped}
    assert names == {"discord": "new-name", "stoat": "200"}


async def test_channel_renamed_leaves_a_non_supporting_connectors_stored_name_untouched(coordinator_parts):
    # A connector without supports_channel_rename (IRC - a channel's id
    # there is its name) never actually renames anything, so its stored
    # channel_name shouldn't be overwritten to claim otherwise.
    coordinator, channel_mappings, _message_sync, _emoji_mappings, _health = coordinator_parts
    await _link(channel_mappings, "general", "discord", "100")
    await _link(channel_mappings, "general", "irc", "300")
    coordinator.register_receiver(FakeReceiver("irc", supports_channel_rename=False))

    await coordinator.handle_channel_renamed("discord", "100", "new-name")

    mapped = await channel_mappings.get_mapped_channels("general")
    names = {m.connector_id: m.channel_name for m in mapped}
    assert names == {"discord": "new-name", "irc": "300"}


async def test_channel_renamed_leaves_a_failed_targets_stored_name_untouched(coordinator_parts):
    # If the rename actually fails on the target, the stored name must stay
    # at the old value - otherwise a later rename to the same new_name would
    # be skipped as a no-op, silently masking the earlier failure forever.
    coordinator, channel_mappings, _message_sync, _emoji_mappings, _health = coordinator_parts
    await _link(channel_mappings, "general", "discord", "100")
    await _link(channel_mappings, "general", "stoat", "200")
    coordinator.register_receiver(
        FakeReceiver("stoat", supports_channel_rename=True, raises=RuntimeError("boom"))
    )

    await coordinator.handle_channel_renamed("discord", "100", "new-name")  # must not raise

    mapped = await channel_mappings.get_mapped_channels("general")
    names = {m.connector_id: m.channel_name for m in mapped}
    assert names == {"discord": "new-name", "stoat": "200"}


async def test_channel_renamed_is_a_noop_for_an_unmapped_channel(coordinator_parts):
    coordinator, _channel_mappings, _message_sync, _emoji_mappings, _health = coordinator_parts
    receiver = FakeReceiver("stoat", supports_channel_rename=True)
    coordinator.register_receiver(receiver)

    await coordinator.handle_channel_renamed("discord", "999", "new-name")

    assert receiver.renames == []


async def test_channel_renamed_skips_a_receiver_without_the_capability(coordinator_parts):
    coordinator, channel_mappings, _message_sync, _emoji_mappings, _health = coordinator_parts
    await _link(channel_mappings, "general", "discord", "100")
    await _link(channel_mappings, "general", "irc", "300")
    irc_receiver = FakeReceiver("irc", supports_channel_rename=False)
    coordinator.register_receiver(irc_receiver)

    await coordinator.handle_channel_renamed("discord", "100", "new-name")  # must not raise

    assert irc_receiver.renames == []


async def test_channel_renamed_echo_from_our_own_write_is_dropped(coordinator_parts):
    coordinator, channel_mappings, _message_sync, _emoji_mappings, _health = coordinator_parts
    await _link(channel_mappings, "general", "discord", "100")
    await _link(channel_mappings, "general", "stoat", "200")
    discord_receiver = FakeReceiver("discord", supports_channel_rename=True)
    stoat_receiver = FakeReceiver("stoat", supports_channel_rename=True)
    coordinator.register_receiver(discord_receiver)
    coordinator.register_receiver(stoat_receiver)

    # discord-origin rename fans out to stoat, recording that write...
    await coordinator.handle_channel_renamed("discord", "100", "new-name")
    assert stoat_receiver.renames == [("200", "new-name")]
    # ...and the stoat side's resulting rename event echoes back but is suppressed.
    await coordinator.handle_channel_renamed("stoat", "200", "new-name")
    assert discord_receiver.renames == []


async def test_channel_renamed_relay_that_raises_is_swallowed(coordinator_parts):
    coordinator, channel_mappings, _message_sync, _emoji_mappings, _health = coordinator_parts
    await _link(channel_mappings, "general", "discord", "100")
    await _link(channel_mappings, "general", "stoat", "200")
    await _link(channel_mappings, "general", "irc", "300")
    failing = FakeReceiver("stoat", supports_channel_rename=True, raises=RuntimeError("boom"))
    working = FakeReceiver("irc", supports_channel_rename=True)
    coordinator.register_receiver(failing)
    coordinator.register_receiver(working)

    await coordinator.handle_channel_renamed("discord", "100", "new-name")  # must not raise

    assert working.renames == [("300", "new-name")]
