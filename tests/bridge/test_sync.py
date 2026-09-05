from __future__ import annotations

from stoat_discord_bridge.models import StandardEdit, StandardPin, StandardTyping
from tests.bridge.conftest import FakeReceiver, _link, _ref


# ---------------------------------------------------------------- handle_pin


async def test_pin_forwards_only_to_connectors_that_support_it(coordinator_parts):
    coordinator, _channel_mappings, message_sync, _emoji_mappings, _health = coordinator_parts
    await message_sync.record(
        "general", _ref("discord", "100", "m1"), [_ref("stoat", "200", "s1"), _ref("irc", "300", "i1")]
    )
    stoat_receiver = FakeReceiver("stoat", supports_pins=True)
    irc_receiver = FakeReceiver("irc", supports_pins=False)
    coordinator.register_receiver(stoat_receiver)
    coordinator.register_receiver(irc_receiver)

    await coordinator.handle_pin(
        StandardPin(origin_connector_id="discord", origin_channel_id="100", origin_message_id="m1", pinned=True)
    )

    assert stoat_receiver.pins == [("200", "s1", True)]
    assert irc_receiver.pins == []


async def test_pin_is_a_noop_for_an_untracked_message(coordinator_parts):
    coordinator, _channel_mappings, _message_sync, _emoji_mappings, _health = coordinator_parts
    receiver = FakeReceiver("stoat", supports_pins=True)
    coordinator.register_receiver(receiver)

    await coordinator.handle_pin(
        StandardPin(origin_connector_id="discord", origin_channel_id="100", origin_message_id="nope", pinned=False)
    )

    assert receiver.pins == []


async def test_pin_echo_from_our_own_write_is_dropped(coordinator_parts):
    coordinator, _channel_mappings, message_sync, _emoji_mappings, _health = coordinator_parts
    await message_sync.record("general", _ref("discord", "100", "m1"), [_ref("stoat", "200", "s1")])
    discord_receiver = FakeReceiver("discord", supports_pins=True)
    stoat_receiver = FakeReceiver("stoat", supports_pins=True)
    coordinator.register_receiver(discord_receiver)
    coordinator.register_receiver(stoat_receiver)

    # discord-origin pin fans out to stoat, recording that write...
    await coordinator.handle_pin(
        StandardPin(origin_connector_id="discord", origin_channel_id="100", origin_message_id="m1", pinned=True)
    )
    assert stoat_receiver.pins == [("200", "s1", True)]
    # ...and the stoat side's resulting pin event echoes back but is suppressed.
    await coordinator.handle_pin(
        StandardPin(origin_connector_id="stoat", origin_channel_id="200", origin_message_id="s1", pinned=True)
    )
    assert discord_receiver.pins == []


async def test_pin_relay_that_raises_is_swallowed(coordinator_parts):
    coordinator, _channel_mappings, message_sync, _emoji_mappings, _health = coordinator_parts
    await message_sync.record(
        "general", _ref("discord", "100", "m1"), [_ref("stoat", "200", "s1"), _ref("irc", "300", "i1")]
    )
    failing = FakeReceiver("stoat", supports_pins=True, raises=RuntimeError("boom"))
    working = FakeReceiver("irc", supports_pins=True)
    coordinator.register_receiver(failing)
    coordinator.register_receiver(working)

    await coordinator.handle_pin(
        StandardPin(origin_connector_id="discord", origin_channel_id="100", origin_message_id="m1", pinned=False)
    )  # must not raise

    assert working.pins == [("300", "i1", False)]


# ---------------------------------------------------------------- handle_edit


async def test_edit_forwards_only_to_connectors_that_support_it(coordinator_parts):
    coordinator, _channel_mappings, message_sync, _emoji_mappings, _health = coordinator_parts
    await message_sync.record(
        "general", _ref("discord", "100", "m1"), [_ref("stoat", "200", "s1"), _ref("irc", "300", "i1")]
    )
    stoat_receiver = FakeReceiver("stoat", supports_edits=True)
    irc_receiver = FakeReceiver("irc", supports_edits=False)
    coordinator.register_receiver(stoat_receiver)
    coordinator.register_receiver(irc_receiver)

    await coordinator.handle_edit(
        StandardEdit(
            origin_connector_id="discord",
            origin_channel_id="100",
            origin_message_id="m1",
            new_content_markdown="fixed typo",
        )
    )

    assert stoat_receiver.edits == [("200", ("s1",), "fixed typo")]
    assert irc_receiver.edits == []


async def test_edit_passes_every_split_post_for_a_channel(coordinator_parts):
    coordinator, _channel_mappings, message_sync, _emoji_mappings, _health = coordinator_parts
    await message_sync.record(
        "general", _ref("discord", "100", "m1"), [_ref("stoat", "200", "s1"), _ref("stoat", "200", "s2")]
    )
    stoat_receiver = FakeReceiver("stoat", supports_edits=True)
    coordinator.register_receiver(stoat_receiver)

    await coordinator.handle_edit(
        StandardEdit(
            origin_connector_id="discord", origin_channel_id="100", origin_message_id="m1", new_content_markdown="x"
        )
    )

    assert stoat_receiver.edits == [("200", ("s1", "s2"), "x")]


async def test_edit_is_a_noop_for_an_untracked_message(coordinator_parts):
    coordinator, _channel_mappings, _message_sync, _emoji_mappings, _health = coordinator_parts
    receiver = FakeReceiver("stoat", supports_edits=True)
    coordinator.register_receiver(receiver)

    await coordinator.handle_edit(
        StandardEdit(
            origin_connector_id="discord", origin_channel_id="100", origin_message_id="nope", new_content_markdown="x"
        )
    )

    assert receiver.edits == []


async def test_edit_echo_from_our_own_write_is_dropped(coordinator_parts):
    coordinator, _channel_mappings, message_sync, _emoji_mappings, _health = coordinator_parts
    await message_sync.record("general", _ref("discord", "100", "m1"), [_ref("stoat", "200", "s1")])
    discord_receiver = FakeReceiver("discord", supports_edits=True)
    stoat_receiver = FakeReceiver("stoat", supports_edits=True)
    coordinator.register_receiver(discord_receiver)
    coordinator.register_receiver(stoat_receiver)

    await coordinator.handle_edit(
        StandardEdit(
            origin_connector_id="discord", origin_channel_id="100", origin_message_id="m1", new_content_markdown="v2"
        )
    )
    assert stoat_receiver.edits == [("200", ("s1",), "v2")]
    # the stoat side's resulting message-update event echoes back but is suppressed
    await coordinator.handle_edit(
        StandardEdit(
            origin_connector_id="stoat", origin_channel_id="200", origin_message_id="s1", new_content_markdown="v2"
        )
    )
    assert discord_receiver.edits == []


async def test_edit_relay_that_raises_is_swallowed(coordinator_parts):
    coordinator, _channel_mappings, message_sync, _emoji_mappings, _health = coordinator_parts
    await message_sync.record(
        "general", _ref("discord", "100", "m1"), [_ref("stoat", "200", "s1"), _ref("irc", "300", "i1")]
    )
    failing = FakeReceiver("stoat", supports_edits=True, raises=RuntimeError("boom"))
    working = FakeReceiver("irc", supports_edits=True)
    coordinator.register_receiver(failing)
    coordinator.register_receiver(working)

    await coordinator.handle_edit(
        StandardEdit(
            origin_connector_id="discord", origin_channel_id="100", origin_message_id="m1", new_content_markdown="x"
        )
    )  # must not raise

    assert working.edits == [("300", ("i1",), "x")]


# ---------------------------------------------------------------- handle_typing


async def test_typing_forwards_only_to_mapped_connectors_that_support_it(coordinator_parts):
    coordinator, channel_mappings, _message_sync, _emoji_mappings, _health = coordinator_parts
    await _link(channel_mappings, "general", "discord", "100")
    await _link(channel_mappings, "general", "stoat", "200")
    await _link(channel_mappings, "general", "irc", "300")
    stoat_receiver = FakeReceiver("stoat", supports_typing=True)
    irc_receiver = FakeReceiver("irc", supports_typing=False)
    coordinator.register_receiver(stoat_receiver)
    coordinator.register_receiver(irc_receiver)

    await coordinator.handle_typing(
        StandardTyping(
            origin_connector_id="discord", origin_channel_id="100", sender_name="Alice", sender_user_id="a"
        )
    )

    assert stoat_receiver.typing == ["200"]
    assert irc_receiver.typing == []


async def test_stopped_typing_routes_to_stop_typing(coordinator_parts):
    coordinator, channel_mappings, _message_sync, _emoji_mappings, _health = coordinator_parts
    await _link(channel_mappings, "general", "discord", "100")
    await _link(channel_mappings, "general", "stoat", "200")
    stoat_receiver = FakeReceiver("stoat", supports_typing=True)
    coordinator.register_receiver(stoat_receiver)

    await coordinator.handle_typing(
        StandardTyping(
            origin_connector_id="discord",
            origin_channel_id="100",
            sender_name="Alice",
            sender_user_id="a",
            active=False,
        )
    )

    assert stoat_receiver.typing == []
    assert stoat_receiver.typing_stopped == ["200"]


async def test_typing_is_a_noop_for_an_unbridged_channel(coordinator_parts):
    coordinator, _channel_mappings, _message_sync, _emoji_mappings, _health = coordinator_parts
    receiver = FakeReceiver("stoat", supports_typing=True)
    coordinator.register_receiver(receiver)

    await coordinator.handle_typing(
        StandardTyping(
            origin_connector_id="discord", origin_channel_id="nope", sender_name="Alice", sender_user_id="a"
        )
    )

    assert receiver.typing == []


async def test_typing_relay_that_raises_is_swallowed(coordinator_parts):
    coordinator, channel_mappings, _message_sync, _emoji_mappings, _health = coordinator_parts
    await _link(channel_mappings, "general", "discord", "100")
    await _link(channel_mappings, "general", "stoat", "200")
    await _link(channel_mappings, "general", "irc", "300")
    failing = FakeReceiver("stoat", supports_typing=True, raises=RuntimeError("boom"))
    working = FakeReceiver("irc", supports_typing=True)
    coordinator.register_receiver(failing)
    coordinator.register_receiver(working)

    await coordinator.handle_typing(
        StandardTyping(
            origin_connector_id="discord", origin_channel_id="100", sender_name="Alice", sender_user_id="a"
        )
    )  # must not raise

    assert working.typing == ["300"]


