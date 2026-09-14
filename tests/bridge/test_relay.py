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

    assert stoat_receiver.received == [(message, "200", None)]
    assert irc_receiver.received == [(message, "300", None)]
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


# ---------------------------------------------------------------- reply resolution (issue #101)


async def test_reply_resolves_to_each_targets_own_counterpart_message_id(coordinator_parts):
    coordinator, channel_mappings, message_sync, _emoji_mappings, _health = coordinator_parts
    await _link(channel_mappings, "general", "discord", "100")
    await _link(channel_mappings, "general", "stoat", "200")
    await _link(channel_mappings, "general", "irc", "300")
    # A prior message, already synced across all three connectors.
    await message_sync.record(
        "general",
        _ref("discord", "100", "original-discord"),
        [_ref("stoat", "200", "original-stoat"), _ref("irc", "300", "original-irc")],
    )

    stoat_receiver = FakeReceiver("stoat", native_ids=["s1"])
    irc_receiver = FakeReceiver("irc", native_ids=["i1"])
    coordinator.register_receiver(stoat_receiver)
    coordinator.register_receiver(irc_receiver)

    reply = _message(reply_to_message_id="original-discord")
    await coordinator.handle_incoming(reply)

    assert stoat_receiver.received == [(reply, "200", "original-stoat")]
    assert irc_receiver.received == [(reply, "300", "original-irc")]


# ---------------------------------------------------------------- find_group_if_origin (issue #134)


async def test_find_group_if_origin_matches_from_the_origin_side(coordinator_parts):
    _coordinator, _channel_mappings, message_sync, _emoji_mappings, _health = coordinator_parts
    await message_sync.record("general", _ref("discord", "100", "m1"), [_ref("stoat", "200", "s1")])

    group = await message_sync.find_group_if_origin("discord", "100", "m1")

    assert group is not None
    assert {(r.connector_id, r.channel_id, r.message_id) for r in group} == {
        ("discord", "100", "m1"),
        ("stoat", "200", "s1"),
    }


async def test_find_group_if_origin_is_none_when_called_from_a_relayed_copy(coordinator_parts):
    _coordinator, _channel_mappings, message_sync, _emoji_mappings, _health = coordinator_parts
    await message_sync.record("general", _ref("discord", "100", "m1"), [_ref("stoat", "200", "s1")])

    assert await message_sync.find_group_if_origin("stoat", "200", "s1") is None


async def test_reply_resolves_when_the_replied_to_message_was_itself_a_relayed_copy(coordinator_parts):
    coordinator, channel_mappings, message_sync, _emoji_mappings, _health = coordinator_parts
    await _link(channel_mappings, "general", "discord", "100")
    await _link(channel_mappings, "general", "stoat", "200")
    # The original message originated on Stoat and was relayed to Discord;
    # find_group must resolve starting from either side of that sync group.
    await message_sync.record(
        "general", _ref("stoat", "200", "original-stoat"), [_ref("discord", "100", "original-discord")]
    )

    stoat_receiver = FakeReceiver("stoat", native_ids=["s1"])
    coordinator.register_receiver(stoat_receiver)

    reply = _message(reply_to_message_id="original-discord")  # replying to the bridge's own Discord copy
    await coordinator.handle_incoming(reply)

    assert stoat_receiver.received == [(reply, "200", "original-stoat")]


async def test_reply_to_a_never_relayed_message_leaves_reply_target_unset(coordinator_parts):
    coordinator, channel_mappings, _message_sync, _emoji_mappings, _health = coordinator_parts
    await _link(channel_mappings, "general", "discord", "100")
    await _link(channel_mappings, "general", "stoat", "200")

    stoat_receiver = FakeReceiver("stoat", native_ids=["s1"])
    coordinator.register_receiver(stoat_receiver)

    reply = _message(reply_to_message_id="never-synced")
    await coordinator.handle_incoming(reply)

    assert stoat_receiver.received == [(reply, "200", None)]


async def test_reply_to_a_message_not_relayed_to_this_particular_target_leaves_it_unset(coordinator_parts):
    coordinator, channel_mappings, message_sync, _emoji_mappings, _health = coordinator_parts
    await _link(channel_mappings, "general", "discord", "100")
    await _link(channel_mappings, "general", "stoat", "200")
    await _link(channel_mappings, "general", "irc", "300")
    # Synced to stoat only (e.g. irc wasn't linked in yet at the time).
    await message_sync.record("general", _ref("discord", "100", "original-discord"), [_ref("stoat", "200", "original-stoat")])

    stoat_receiver = FakeReceiver("stoat", native_ids=["s1"])
    irc_receiver = FakeReceiver("irc", native_ids=["i1"])
    coordinator.register_receiver(stoat_receiver)
    coordinator.register_receiver(irc_receiver)

    reply = _message(reply_to_message_id="original-discord")
    await coordinator.handle_incoming(reply)

    assert stoat_receiver.received == [(reply, "200", "original-stoat")]
    assert irc_receiver.received == [(reply, "300", None)]


async def test_non_reply_message_passes_no_reply_target(coordinator_parts):
    coordinator, channel_mappings, _message_sync, _emoji_mappings, _health = coordinator_parts
    await _link(channel_mappings, "general", "discord", "100")
    await _link(channel_mappings, "general", "stoat", "200")

    stoat_receiver = FakeReceiver("stoat", native_ids=["s1"])
    coordinator.register_receiver(stoat_receiver)

    await coordinator.handle_incoming(_message())

    assert stoat_receiver.received[0][2] is None


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


