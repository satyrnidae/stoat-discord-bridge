"""A relay target whose channel was deleted on its platform (the receiver
raises RelayTargetGoneError) has its stale mapping pruned on the spot,
instead of failing the same way on every later message (issue #217)."""

from __future__ import annotations

import logging

from stoat_discord_bridge.admin_commands import ConnectorInfo
from stoat_discord_bridge.bridge import BridgeCoordinator
from stoat_discord_bridge.models import StandardDelete, StandardEdit, StandardPin, StandardReaction
from stoat_discord_bridge.services.base import RelayTargetGoneError
from tests.bridge.conftest import FakeReceiver, _link, _message, _ref


def _gone(channel_id: str = "200") -> RelayTargetGoneError:
    return RelayTargetGoneError(f"channel {channel_id} was deleted")


async def _linked_ids(channel_mappings, group: str) -> set[tuple[str, str]]:
    return {(m.connector_id, m.channel_id) for m in await channel_mappings.get_mapped_channels(group)}


async def test_a_gone_target_is_pruned_and_the_rest_of_the_group_kept(coordinator_parts, caplog):
    coordinator, channel_mappings, message_sync, _emoji, _health = coordinator_parts
    await _link(channel_mappings, "general", "discord", "100")
    await _link(channel_mappings, "general", "stoat", "200")
    await _link(channel_mappings, "general", "irc", "300")
    coordinator.register_receiver(FakeReceiver("stoat", raises=_gone()))
    coordinator.register_receiver(FakeReceiver("irc", native_ids=["i1"]))

    with caplog.at_level(logging.WARNING):
        await coordinator.handle_incoming(_message())

    assert await _linked_ids(channel_mappings, "general") == {("discord", "100"), ("irc", "300")}
    group = await message_sync.find_group("discord", "100", "m1")
    assert {(r.connector_id, r.message_id) for r in group} == {("discord", "m1"), ("irc", "i1")}
    gone_logs = [r for r in caplog.records if "deleted" in r.getMessage()]
    assert gone_logs and all(r.levelno == logging.WARNING and r.exc_info is None for r in gone_logs)


async def test_a_group_left_with_one_member_is_dissolved(coordinator_parts):
    coordinator, channel_mappings, _sync, _emoji, _health = coordinator_parts
    await _link(channel_mappings, "general", "discord", "100")
    await _link(channel_mappings, "general", "stoat", "200")
    coordinator.register_receiver(FakeReceiver("stoat", raises=_gone()))

    await coordinator.handle_incoming(_message())

    assert await channel_mappings.get_mapped_channels("general") == []
    assert await channel_mappings.get_bridge_group("discord", "100") is None


async def test_two_gone_targets_in_one_fan_out_still_dissolve_the_lone_survivor(coordinator_parts):
    coordinator, channel_mappings, _sync, _emoji, _health = coordinator_parts
    await _link(channel_mappings, "general", "discord", "100")
    await _link(channel_mappings, "general", "stoat", "200")
    await _link(channel_mappings, "general", "irc", "300")
    coordinator.register_receiver(FakeReceiver("stoat", raises=_gone()))
    coordinator.register_receiver(FakeReceiver("irc", raises=_gone("300")))

    await coordinator.handle_incoming(_message())

    assert await channel_mappings.get_mapped_channels("general") == []


async def test_a_dissolved_survivor_is_told_it_was_unlinked(coordinator_parts):
    _coordinator, channel_mappings, message_sync, emoji_mappings, health = coordinator_parts
    unlinked: list[tuple[str, str]] = []

    async def on_channel_unlinked(channel_id: str, unlinked_from: str) -> None:
        unlinked.append((channel_id, unlinked_from))

    connectors = {
        "discord": ConnectorInfo(id="discord", label="Discord"),
        "irc": ConnectorInfo(id="irc", label="IRC", on_channel_unlinked=on_channel_unlinked),
    }
    coordinator = BridgeCoordinator(channel_mappings, message_sync, emoji_mappings, health, connectors)
    await _link(channel_mappings, "general", "irc", "#general")
    await _link(channel_mappings, "general", "discord", "100")
    coordinator.register_receiver(FakeReceiver("discord", raises=_gone("100")))

    await coordinator.handle_incoming(_message(origin_connector_id="irc", origin_channel_id="#general"))

    assert unlinked == [("#general", "Discord '100'")]


async def test_a_backfill_into_a_gone_channel_stops(coordinator_parts):
    coordinator, _mappings, _sync, _emoji, _health = coordinator_parts
    receiver = FakeReceiver("stoat", raises=_gone())
    coordinator.register_receiver(receiver)

    async def fetch_history(_channel_id, _limit):
        return [_message(message_id="m1"), _message(message_id="m2")]

    summary = await coordinator.backfill_history(
        fetch_history=fetch_history,
        source_channel_id="100",
        destination_connector="stoat",
        destination_channel_id="200",
        limit=None,
    )

    assert len(receiver.received) == 1
    assert "stopped" in summary


async def test_any_other_failure_leaves_the_mapping_alone(coordinator_parts):
    coordinator, channel_mappings, _sync, _emoji, _health = coordinator_parts
    await _link(channel_mappings, "general", "discord", "100")
    await _link(channel_mappings, "general", "stoat", "200")
    coordinator.register_receiver(FakeReceiver("stoat", raises=RuntimeError("missing permissions")))

    await coordinator.handle_incoming(_message())

    assert await _linked_ids(channel_mappings, "general") == {("discord", "100"), ("stoat", "200")}


async def _tracked(coordinator_parts, receiver: FakeReceiver):
    coordinator, channel_mappings, message_sync, _emoji, _health = coordinator_parts
    await _link(channel_mappings, "general", "discord", "100")
    await _link(channel_mappings, "general", "stoat", "200")
    await _link(channel_mappings, "general", "irc", "300")
    await message_sync.record("general", _ref("discord", "100", "m1"), [_ref("stoat", "200", "s1")])
    coordinator.register_receiver(receiver)
    return coordinator, channel_mappings


async def test_edit_into_a_gone_channel_prunes_it(coordinator_parts):
    coordinator, channel_mappings = await _tracked(
        coordinator_parts, FakeReceiver("stoat", supports_edits=True, raises=_gone())
    )

    await coordinator.handle_edit(
        StandardEdit(
            origin_connector_id="discord", origin_channel_id="100", origin_message_id="m1", new_content_markdown="x"
        )
    )

    assert await _linked_ids(channel_mappings, "general") == {("discord", "100"), ("irc", "300")}


async def test_delete_into_a_gone_channel_prunes_it(coordinator_parts):
    coordinator, channel_mappings = await _tracked(
        coordinator_parts, FakeReceiver("stoat", supports_deletes=True, raises=_gone())
    )

    await coordinator.handle_delete(
        StandardDelete(origin_connector_id="discord", origin_channel_id="100", origin_message_id="m1")
    )

    assert await _linked_ids(channel_mappings, "general") == {("discord", "100"), ("irc", "300")}


async def test_pin_into_a_gone_channel_prunes_it(coordinator_parts):
    coordinator, channel_mappings = await _tracked(
        coordinator_parts, FakeReceiver("stoat", supports_pins=True, raises=_gone())
    )

    await coordinator.handle_pin(
        StandardPin(origin_connector_id="discord", origin_channel_id="100", origin_message_id="m1", pinned=True)
    )

    assert await _linked_ids(channel_mappings, "general") == {("discord", "100"), ("irc", "300")}


async def test_reaction_into_a_gone_channel_prunes_it(coordinator_parts):
    coordinator, channel_mappings = await _tracked(
        coordinator_parts, FakeReceiver("stoat", supports_reactions=True, raises=_gone())
    )

    await coordinator.handle_reaction(
        StandardReaction(
            origin_connector_id="discord", origin_channel_id="100", origin_message_id="m1", emoji="x", added=True
        )
    )

    assert await _linked_ids(channel_mappings, "general") == {("discord", "100"), ("irc", "300")}


async def test_a_gone_channel_already_unlinked_is_a_no_op(coordinator_parts):
    # The relay raced an /unlink (or another prune) - nothing left to drop.
    coordinator, channel_mappings, message_sync, _emoji, _health = coordinator_parts
    await _link(channel_mappings, "general", "discord", "100")
    await message_sync.record("general", _ref("discord", "100", "m1"), [_ref("stoat", "200", "s1")])
    coordinator.register_receiver(FakeReceiver("stoat", supports_pins=True, raises=_gone()))

    await coordinator.handle_pin(
        StandardPin(origin_connector_id="discord", origin_channel_id="100", origin_message_id="m1", pinned=True)
    )

    assert await _linked_ids(channel_mappings, "general") == {("discord", "100")}
