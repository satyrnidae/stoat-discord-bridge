from __future__ import annotations

from stoat_discord_bridge.models import CustomEmoji, StandardEmojiCreated, StandardEmojiDeleted, StandardReaction
from stoat_discord_bridge.services.base import PartialRelayError, UnsupportedRelayTargetError
from stoat_discord_bridge.status import HealthState
from tests.bridge.conftest import FakeReceiver, _emoji_ref, _link, _message, _ref


# ---------------------------------------------------------------- handle_incoming


async def test_relays_to_every_other_mapped_connector_and_records_sync(coordinator_parts):
    coordinator, channel_mappings, message_sync, _emoji_mappings, health = coordinator_parts
    await _link(channel_mappings, "general", "discord", "100")
    await _link(channel_mappings, "general", "stoat", "200")
    await _link(channel_mappings, "general", "irc", "300")

    stoat_receiver = FakeReceiver("stoat", native_ids=["s1"])
    irc_receiver = FakeReceiver("irc", native_ids=["i1"])
    coordinator.register_receiver(stoat_receiver)
    coordinator.register_receiver(irc_receiver)

    message = _message()
    await coordinator.handle_incoming(message)

    assert stoat_receiver.received == [(message, "200")]
    assert irc_receiver.received == [(message, "300")]
    assert health.snapshot()["stoat"] == HealthState.HEALTHY
    assert health.snapshot()["irc"] == HealthState.HEALTHY

    group = await message_sync.find_group("discord", "100", "m1")
    assert group is not None
    assert {(r.connector_id, r.channel_id, r.message_id) for r in group} == {
        ("discord", "100", "m1"),
        ("stoat", "200", "s1"),
        ("irc", "300", "i1"),
    }


async def test_does_nothing_when_the_origin_channel_isnt_bridged(coordinator_parts):
    coordinator, _channel_mappings, message_sync, _emoji_mappings, _health = coordinator_parts

    await coordinator.handle_incoming(_message(origin_channel_id="unlinked"))

    assert await message_sync.find_group("discord", "unlinked", "m1") is None


async def test_a_target_with_no_registered_receiver_is_dropped_not_fatal(coordinator_parts):
    coordinator, channel_mappings, message_sync, _emoji_mappings, _health = coordinator_parts
    await _link(channel_mappings, "general", "discord", "100")
    await _link(channel_mappings, "general", "stoat", "200")
    # deliberately no receiver registered for "stoat"

    await coordinator.handle_incoming(_message())

    assert await message_sync.find_group("discord", "100", "m1") is None


async def test_partial_relay_error_records_only_the_ids_delivered_before_failure(coordinator_parts):
    coordinator, channel_mappings, message_sync, _emoji_mappings, health = coordinator_parts
    await _link(channel_mappings, "general", "discord", "100")
    await _link(channel_mappings, "general", "stoat", "200")

    receiver = FakeReceiver("stoat", raises=PartialRelayError(["only-1"], RuntimeError("boom")))
    coordinator.register_receiver(receiver)

    await coordinator.handle_incoming(_message())

    group = await message_sync.find_group("discord", "100", "m1")
    assert {(r.connector_id, r.message_id) for r in group} == {("discord", "m1"), ("stoat", "only-1")}
    assert health.snapshot()["stoat"] == HealthState.DEGRADED


async def test_unsupported_relay_target_is_dropped_and_records_nothing(coordinator_parts):
    coordinator, channel_mappings, message_sync, _emoji_mappings, health = coordinator_parts
    await _link(channel_mappings, "general", "discord", "100")
    await _link(channel_mappings, "general", "stoat", "200")

    receiver = FakeReceiver(
        "stoat", raises=UnsupportedRelayTargetError("channel 200 is a forum/media channel")
    )
    coordinator.register_receiver(receiver)

    await coordinator.handle_incoming(_message())

    assert await message_sync.find_group("discord", "100", "m1") is None
    assert health.snapshot()["stoat"] == HealthState.DEGRADED


async def test_a_failing_target_is_dropped_while_others_still_relay(coordinator_parts):
    coordinator, channel_mappings, message_sync, _emoji_mappings, health = coordinator_parts
    await _link(channel_mappings, "general", "discord", "100")
    await _link(channel_mappings, "general", "stoat", "200")
    await _link(channel_mappings, "general", "irc", "300")

    failing = FakeReceiver("stoat", raises=RuntimeError("connection reset"))
    working = FakeReceiver("irc", native_ids=["i1"])
    coordinator.register_receiver(failing)
    coordinator.register_receiver(working)

    await coordinator.handle_incoming(_message())

    group = await message_sync.find_group("discord", "100", "m1")
    assert {(r.connector_id, r.message_id) for r in group} == {("discord", "m1"), ("irc", "i1")}
    assert health.snapshot()["stoat"] == HealthState.DEGRADED
    assert health.snapshot()["irc"] == HealthState.HEALTHY


# ---------------------------------------------------------------- handle_reaction


async def test_reaction_forwards_only_to_connectors_that_support_it(coordinator_parts):
    coordinator, _channel_mappings, message_sync, _emoji_mappings, _health = coordinator_parts
    await message_sync.record(
        "general",
        _ref("discord", "100", "m1"),
        [_ref("stoat", "200", "s1"), _ref("irc", "300", "i1")],
    )
    stoat_receiver = FakeReceiver("stoat", supports_reactions=True)
    irc_receiver = FakeReceiver("irc", supports_reactions=False)
    coordinator.register_receiver(stoat_receiver)
    coordinator.register_receiver(irc_receiver)

    reaction = StandardReaction(
        origin_connector_id="discord", origin_channel_id="100", origin_message_id="m1", emoji="\U0001f600", added=True
    )
    await coordinator.handle_reaction(reaction)

    assert stoat_receiver.reactions == [("add", "200", "s1", "\U0001f600")]
    assert irc_receiver.reactions == []


async def test_reaction_add_skipped_when_another_origin_user_already_reacted(coordinator_parts):
    coordinator, _channel_mappings, message_sync, _emoji_mappings, _health = coordinator_parts
    await message_sync.record("general", _ref("discord", "100", "m1"), [_ref("stoat", "200", "s1")])
    receiver = FakeReceiver("stoat", supports_reactions=True)
    coordinator.register_receiver(receiver)

    await coordinator.handle_reaction(
        StandardReaction(
            origin_connector_id="discord",
            origin_channel_id="100",
            origin_message_id="m1",
            emoji="\U0001f600",
            added=True,
            origin_reactor_count=2,
        )
    )

    assert receiver.reactions == []


async def test_reaction_remove_held_until_last_origin_reactor_leaves(coordinator_parts):
    coordinator, _channel_mappings, message_sync, _emoji_mappings, _health = coordinator_parts
    await message_sync.record("general", _ref("discord", "100", "m1"), [_ref("stoat", "200", "s1")])
    receiver = FakeReceiver("stoat", supports_reactions=True)
    coordinator.register_receiver(receiver)

    def _remove(count):
        return StandardReaction(
            origin_connector_id="discord",
            origin_channel_id="100",
            origin_message_id="m1",
            emoji="\U0001f600",
            added=False,
            origin_reactor_count=count,
        )

    await coordinator.handle_reaction(_remove(1))  # one user still holds it
    assert receiver.reactions == []

    await coordinator.handle_reaction(_remove(0))  # last one gone
    assert receiver.reactions == [("remove", "200", "s1", "\U0001f600")]


async def test_reaction_is_a_noop_for_an_untracked_message(coordinator_parts):
    coordinator, _channel_mappings, _message_sync, _emoji_mappings, _health = coordinator_parts
    receiver = FakeReceiver("stoat", supports_reactions=True)
    coordinator.register_receiver(receiver)

    reaction = StandardReaction(
        origin_connector_id="discord", origin_channel_id="100", origin_message_id="never-relayed", emoji="x", added=True
    )
    await coordinator.handle_reaction(reaction)

    assert receiver.reactions == []


async def test_reaction_with_a_custom_emoji_translates_via_the_emoji_mapping(coordinator_parts):
    coordinator, _channel_mappings, message_sync, emoji_mappings, _health = coordinator_parts
    await message_sync.record("general", _ref("discord", "100", "m1"), [_ref("stoat", "200", "s1")])
    group_id = await emoji_mappings.try_reserve(_emoji_ref("discord", "e1"))
    await emoji_mappings.add_refs(group_id, [_emoji_ref("stoat", "e1s")])

    receiver = FakeReceiver("stoat", supports_reactions=True)
    coordinator.register_receiver(receiver)

    reaction = StandardReaction(
        origin_connector_id="discord",
        origin_channel_id="100",
        origin_message_id="m1",
        emoji=CustomEmoji(native_id="e1", name="smile", image_url="https://cdn.example/e1.png"),
        added=False,
    )
    await coordinator.handle_reaction(reaction)

    assert len(receiver.reactions) == 1
    kind, channel_id, message_id, emoji = receiver.reactions[0]
    assert (kind, channel_id, message_id) == ("remove", "200", "s1")
    assert emoji.native_id == "e1s"


async def test_reaction_relay_that_raises_is_swallowed_not_propagated(coordinator_parts):
    coordinator, _channel_mappings, message_sync, _emoji_mappings, _health = coordinator_parts
    await message_sync.record(
        "general", _ref("discord", "100", "m1"), [_ref("stoat", "200", "s1"), _ref("irc", "300", "i1")]
    )
    failing = FakeReceiver("stoat", supports_reactions=True, raises=RuntimeError("boom"))
    working = FakeReceiver("irc", supports_reactions=True)
    coordinator.register_receiver(failing)
    coordinator.register_receiver(working)

    reaction = StandardReaction(
        origin_connector_id="discord", origin_channel_id="100", origin_message_id="m1", emoji="x", added=True
    )
    await coordinator.handle_reaction(reaction)  # must not raise

    assert working.reactions == [("add", "300", "i1", "x")]


async def test_reaction_with_a_custom_emoji_never_mirrored_to_the_target_is_skipped(coordinator_parts):
    coordinator, _channel_mappings, message_sync, _emoji_mappings, _health = coordinator_parts
    await message_sync.record("general", _ref("discord", "100", "m1"), [_ref("stoat", "200", "s1")])
    receiver = FakeReceiver("stoat", supports_reactions=True)
    coordinator.register_receiver(receiver)

    reaction = StandardReaction(
        origin_connector_id="discord",
        origin_channel_id="100",
        origin_message_id="m1",
        emoji=CustomEmoji(native_id="never-mirrored", name="smile", image_url="https://cdn.example/e1.png"),
        added=True,
    )
    await coordinator.handle_reaction(reaction)

    assert receiver.reactions == []


# ---------------------------------------------------------------- handle_emoji_created / deleted


async def test_emoji_created_mirrors_only_to_connectors_that_support_it(coordinator_parts):
    coordinator, _channel_mappings, _message_sync, emoji_mappings, _health = coordinator_parts
    mirrored = CustomEmoji(native_id="stoat-e1", name="smile", image_url="https://cdn.example/stoat-e1.png")
    stoat_receiver = FakeReceiver("stoat", supports_emoji=True, created_emoji=mirrored)
    irc_receiver = FakeReceiver("irc", supports_emoji=False)
    coordinator.register_receiver(stoat_receiver)
    coordinator.register_receiver(irc_receiver)

    created = StandardEmojiCreated(
        origin_connector_id="discord",
        emoji=CustomEmoji(native_id="e1", name="smile", image_url="https://cdn.example/e1.png"),
    )
    await coordinator.handle_emoji_created(created)

    assert stoat_receiver.created_calls == [created.emoji]
    assert irc_receiver.created_calls == []
    assert await emoji_mappings.find_equivalent("discord", "e1", "stoat") == "stoat-e1"


async def test_emoji_created_is_idempotent_for_a_duplicate_event(coordinator_parts):
    coordinator, _channel_mappings, _message_sync, _emoji_mappings, _health = coordinator_parts
    mirrored = CustomEmoji(native_id="stoat-e1", name="smile", image_url="https://cdn.example/stoat-e1.png")
    receiver = FakeReceiver("stoat", supports_emoji=True, created_emoji=mirrored)
    coordinator.register_receiver(receiver)

    created = StandardEmojiCreated(
        origin_connector_id="discord",
        emoji=CustomEmoji(native_id="e1", name="smile", image_url="https://cdn.example/e1.png"),
    )
    await coordinator.handle_emoji_created(created)
    await coordinator.handle_emoji_created(created)

    assert len(receiver.created_calls) == 1


async def test_emoji_created_releases_the_reservation_when_no_connector_can_create_it(coordinator_parts):
    coordinator, _channel_mappings, _message_sync, emoji_mappings, _health = coordinator_parts
    receiver = FakeReceiver("stoat", supports_emoji=True, created_emoji=None)
    coordinator.register_receiver(receiver)

    created = StandardEmojiCreated(
        origin_connector_id="discord",
        emoji=CustomEmoji(native_id="e1", name="smile", image_url="https://cdn.example/e1.png"),
    )
    await coordinator.handle_emoji_created(created)
    assert await emoji_mappings.find_equivalent("discord", "e1", "stoat") is None

    # the reservation was released, not left dangling - a retry can still succeed
    receiver._created_emoji = CustomEmoji(native_id="stoat-e1", name="smile", image_url="https://cdn.example/stoat-e1.png")
    await coordinator.handle_emoji_created(created)
    assert await emoji_mappings.find_equivalent("discord", "e1", "stoat") == "stoat-e1"


async def test_emoji_created_mirror_that_raises_is_swallowed_and_others_still_mirror(coordinator_parts):
    coordinator, _channel_mappings, _message_sync, emoji_mappings, _health = coordinator_parts
    mirrored = CustomEmoji(native_id="irc-e1", name="smile", image_url="https://cdn.example/irc-e1.png")
    failing = FakeReceiver("stoat", supports_emoji=True, raises=RuntimeError("slots full"))
    working = FakeReceiver("irc", supports_emoji=True, created_emoji=mirrored)
    coordinator.register_receiver(failing)
    coordinator.register_receiver(working)

    created = StandardEmojiCreated(
        origin_connector_id="discord",
        emoji=CustomEmoji(native_id="e1", name="smile", image_url="https://cdn.example/e1.png"),
    )
    await coordinator.handle_emoji_created(created)  # must not raise

    assert await emoji_mappings.find_equivalent("discord", "e1", "stoat") is None
    assert await emoji_mappings.find_equivalent("discord", "e1", "irc") == "irc-e1"


async def test_emoji_deleted_forgets_only_the_deleted_connectors_ref(coordinator_parts):
    coordinator, _channel_mappings, _message_sync, emoji_mappings, _health = coordinator_parts
    group_id = await emoji_mappings.try_reserve(_emoji_ref("discord", "e1"))
    await emoji_mappings.add_refs(group_id, [_emoji_ref("stoat", "e1s")])

    await coordinator.handle_emoji_deleted(StandardEmojiDeleted(origin_connector_id="discord", native_id="e1"))

    assert await emoji_mappings.get_group_id("discord", "e1") is None
    assert await emoji_mappings.get_group_id("stoat", "e1s") is not None


