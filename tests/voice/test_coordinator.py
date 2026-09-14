from __future__ import annotations

from stoat_discord_bridge.services.voice.coordinator import VoiceBridgeCoordinator
from stoat_discord_bridge.storage.channel_mappings import ChannelMapping, ChannelMappingRepository
from tests.voice.conftest import make_connector


async def _link(mappings: ChannelMappingRepository, group: str, connector_id: str, channel_id: str) -> None:
    await mappings.upsert(
        ChannelMapping(bridge_group=group, connector_id=connector_id, channel_id=channel_id, channel_name=channel_id)
    )


async def test_group_with_two_voice_channels_is_voice_bridgeable(fake_db):
    mappings = ChannelMappingRepository(fake_db)
    await _link(mappings, "g", "discord", "d-vc")
    await _link(mappings, "g", "stoat", "s-vc")
    connectors = {
        "discord": make_connector("discord", voice_channels={"d-vc"}),
        "stoat": make_connector("stoat", voice_channels={"s-vc"}),
    }
    coord = VoiceBridgeCoordinator(mappings, connectors)
    await coord.refresh_groups()
    assert coord.voice_bridgeable_groups() == {"g": {"discord": "d-vc", "stoat": "s-vc"}}


async def test_group_with_only_one_voice_channel_is_not_bridgeable(fake_db):
    mappings = ChannelMappingRepository(fake_db)
    await _link(mappings, "g", "discord", "d-vc")
    await _link(mappings, "g", "irc", "#g")
    connectors = {
        "discord": make_connector("discord", voice_channels={"d-vc"}),
        "irc": make_connector("irc", no_voice_hook=True),
    }
    coord = VoiceBridgeCoordinator(mappings, connectors)
    await coord.refresh_groups()
    assert coord.voice_bridgeable_groups() == {}


async def test_mixed_text_and_voice_members_only_voice_ones_count(fake_db):
    mappings = ChannelMappingRepository(fake_db)
    await _link(mappings, "g", "discord", "d-vc")
    await _link(mappings, "g", "stoat", "s-vc")
    await _link(mappings, "g", "irc", "#g")
    connectors = {
        "discord": make_connector("discord", voice_channels={"d-vc"}),
        "stoat": make_connector("stoat", voice_channels={"s-vc"}),
        "irc": make_connector("irc", no_voice_hook=True),
    }
    coord = VoiceBridgeCoordinator(mappings, connectors)
    await coord.refresh_groups()
    assert coord.voice_bridgeable_groups() == {"g": {"discord": "d-vc", "stoat": "s-vc"}}


async def test_channel_is_voice_exception_treated_as_not_voice(fake_db):
    mappings = ChannelMappingRepository(fake_db)
    await _link(mappings, "g", "discord", "d-vc")
    await _link(mappings, "g", "stoat", "s-vc")
    connectors = {
        "discord": make_connector("discord", voice_channels={"d-vc"}, channel_is_voice_raises=True),
        "stoat": make_connector("stoat", voice_channels={"s-vc"}),
    }
    coord = VoiceBridgeCoordinator(mappings, connectors)
    await coord.refresh_groups()
    assert coord.voice_bridgeable_groups() == {}


async def test_multiple_voice_channels_on_one_connector_keeps_lowest_id(fake_db):
    mappings = ChannelMappingRepository(fake_db)
    await _link(mappings, "g", "discord", "d-vc-2")
    await _link(mappings, "g", "discord", "d-vc-1")
    await _link(mappings, "g", "stoat", "s-vc")
    connectors = {
        "discord": make_connector("discord", voice_channels={"d-vc-1", "d-vc-2"}),
        "stoat": make_connector("stoat", voice_channels={"s-vc"}),
    }
    coord = VoiceBridgeCoordinator(mappings, connectors)
    await coord.refresh_groups()
    assert coord.voice_bridgeable_groups() == {"g": {"discord": "d-vc-1", "stoat": "s-vc"}}


async def test_seeding_reads_current_occupants_on_refresh(fake_db):
    mappings = ChannelMappingRepository(fake_db)
    await _link(mappings, "g", "discord", "d-vc")
    await _link(mappings, "g", "stoat", "s-vc")
    connectors = {
        "discord": make_connector("discord", voice_channels={"d-vc"}, occupants={"d-vc": {"u1"}}),
        "stoat": make_connector("stoat", voice_channels={"s-vc"}, occupants={"s-vc": {"u2"}}),
    }
    coord = VoiceBridgeCoordinator(mappings, connectors)
    await coord.refresh_groups()
    assert coord.occupants("g") == {"discord": frozenset({"u1"}), "stoat": frozenset({"u2"})}


async def test_seeding_none_occupants_leaves_existing_presence_untouched(fake_db):
    mappings = ChannelMappingRepository(fake_db)
    await _link(mappings, "g", "discord", "d-vc")
    await _link(mappings, "g", "stoat", "s-vc")
    connectors = {
        "discord": make_connector("discord", voice_channels={"d-vc"}, occupants={"d-vc": {"u1"}}),
        "stoat": make_connector("stoat", voice_channels={"s-vc"}, occupants={"s-vc": None}),
    }
    coord = VoiceBridgeCoordinator(mappings, connectors)
    await coord.refresh_groups()  # establishes classification so the push below resolves to group "g"
    await coord.on_voice_presence("stoat", "s-vc", "u2", present=True, is_bot=False)
    await coord.refresh_groups()
    # stoat's voice_occupants returns None ("can't tell") - the live-pushed
    # presence from on_voice_presence must survive this second refresh untouched.
    assert coord.occupants("g") == {"discord": frozenset({"u1"}), "stoat": frozenset({"u2"})}


async def test_two_populated_connectors_opens_a_session(fake_db):
    mappings = ChannelMappingRepository(fake_db)
    await _link(mappings, "g", "discord", "d-vc")
    await _link(mappings, "g", "stoat", "s-vc")
    connectors = {
        "discord": make_connector("discord", voice_channels={"d-vc"}),
        "stoat": make_connector("stoat", voice_channels={"s-vc"}),
    }
    coord = VoiceBridgeCoordinator(mappings, connectors)
    await coord.refresh_groups()
    assert coord.active_group is None

    await coord.on_voice_presence("discord", "d-vc", "u1", present=True, is_bot=False)
    assert coord.active_group is None  # only one populated connector so far

    await coord.on_voice_presence("stoat", "s-vc", "u2", present=True, is_bot=False)
    assert coord.active_group == "g"
    assert coord.active_connectors == frozenset({"discord", "stoat"})


async def test_bot_presence_never_counts(fake_db):
    mappings = ChannelMappingRepository(fake_db)
    await _link(mappings, "g", "discord", "d-vc")
    await _link(mappings, "g", "stoat", "s-vc")
    connectors = {
        "discord": make_connector("discord", voice_channels={"d-vc"}),
        "stoat": make_connector("stoat", voice_channels={"s-vc"}),
    }
    coord = VoiceBridgeCoordinator(mappings, connectors)
    await coord.refresh_groups()

    await coord.on_voice_presence("discord", "d-vc", "some-bot", present=True, is_bot=True)
    await coord.on_voice_presence("stoat", "s-vc", "u2", present=True, is_bot=False)
    assert coord.active_group is None


async def test_first_eligible_group_wins_and_session_does_not_switch(fake_db):
    mappings = ChannelMappingRepository(fake_db)
    await _link(mappings, "a", "discord", "d-a")
    await _link(mappings, "a", "stoat", "s-a")
    await _link(mappings, "b", "discord", "d-b")
    await _link(mappings, "b", "stoat", "s-b")
    connectors = {
        "discord": make_connector("discord", voice_channels={"d-a", "d-b"}),
        "stoat": make_connector("stoat", voice_channels={"s-a", "s-b"}),
    }
    coord = VoiceBridgeCoordinator(mappings, connectors)
    await coord.refresh_groups()

    await coord.on_voice_presence("discord", "d-b", "u1", present=True, is_bot=False)
    await coord.on_voice_presence("stoat", "s-b", "u2", present=True, is_bot=False)
    assert coord.active_group == "b"

    # group "a" becoming eligible too must not steal the live session.
    await coord.on_voice_presence("discord", "d-a", "u3", present=True, is_bot=False)
    await coord.on_voice_presence("stoat", "s-a", "u4", present=True, is_bot=False)
    assert coord.active_group == "b"


async def test_third_connector_joins_mid_session(fake_db):
    mappings = ChannelMappingRepository(fake_db)
    await _link(mappings, "g", "discord", "d-vc")
    await _link(mappings, "g", "stoat", "s-vc")
    await _link(mappings, "g", "stoat-sh", "sh-vc")
    connectors = {
        "discord": make_connector("discord", voice_channels={"d-vc"}),
        "stoat": make_connector("stoat", voice_channels={"s-vc"}),
        "stoat-sh": make_connector("stoat-sh", voice_channels={"sh-vc"}),
    }
    coord = VoiceBridgeCoordinator(mappings, connectors)
    await coord.refresh_groups()
    await coord.on_voice_presence("discord", "d-vc", "u1", present=True, is_bot=False)
    await coord.on_voice_presence("stoat", "s-vc", "u2", present=True, is_bot=False)
    assert coord.active_connectors == frozenset({"discord", "stoat"})

    await coord.on_voice_presence("stoat-sh", "sh-vc", "u3", present=True, is_bot=False)
    assert coord.active_connectors == frozenset({"discord", "stoat", "stoat-sh"})


async def test_one_connector_emptying_while_two_remain_just_parts_it(fake_db):
    mappings = ChannelMappingRepository(fake_db)
    await _link(mappings, "g", "discord", "d-vc")
    await _link(mappings, "g", "stoat", "s-vc")
    await _link(mappings, "g", "stoat-sh", "sh-vc")
    connectors = {
        "discord": make_connector("discord", voice_channels={"d-vc"}),
        "stoat": make_connector("stoat", voice_channels={"s-vc"}),
        "stoat-sh": make_connector("stoat-sh", voice_channels={"sh-vc"}),
    }
    coord = VoiceBridgeCoordinator(mappings, connectors)
    await coord.refresh_groups()
    for connector_id, channel_id, user_id in [
        ("discord", "d-vc", "u1"),
        ("stoat", "s-vc", "u2"),
        ("stoat-sh", "sh-vc", "u3"),
    ]:
        await coord.on_voice_presence(connector_id, channel_id, user_id, present=True, is_bot=False)
    assert coord.active_group == "g"

    await coord.on_voice_presence("stoat-sh", "sh-vc", "u3", present=False, is_bot=False)
    assert coord.active_group == "g"
    assert coord.active_connectors == frozenset({"discord", "stoat"})


async def test_dropping_below_two_closes_the_session(fake_db):
    mappings = ChannelMappingRepository(fake_db)
    await _link(mappings, "g", "discord", "d-vc")
    await _link(mappings, "g", "stoat", "s-vc")
    connectors = {
        "discord": make_connector("discord", voice_channels={"d-vc"}),
        "stoat": make_connector("stoat", voice_channels={"s-vc"}),
    }
    coord = VoiceBridgeCoordinator(mappings, connectors)
    await coord.refresh_groups()
    await coord.on_voice_presence("discord", "d-vc", "u1", present=True, is_bot=False)
    await coord.on_voice_presence("stoat", "s-vc", "u2", present=True, is_bot=False)
    assert coord.active_group == "g"

    await coord.on_voice_presence("discord", "d-vc", "u1", present=False, is_bot=False)
    assert coord.active_group is None
    assert coord.active_connectors == frozenset()


async def test_follow_on_empty_off_by_default_stays_closed(fake_db):
    mappings = ChannelMappingRepository(fake_db)
    await _link(mappings, "a", "discord", "d-a")
    await _link(mappings, "a", "stoat", "s-a")
    await _link(mappings, "b", "discord", "d-b")
    await _link(mappings, "b", "stoat", "s-b")
    connectors = {
        "discord": make_connector("discord", voice_channels={"d-a", "d-b"}),
        "stoat": make_connector("stoat", voice_channels={"s-a", "s-b"}),
    }
    coord = VoiceBridgeCoordinator(mappings, connectors)
    await coord.refresh_groups()
    await coord.on_voice_presence("discord", "d-b", "u1", present=True, is_bot=False)
    await coord.on_voice_presence("stoat", "s-b", "u2", present=True, is_bot=False)
    await coord.on_voice_presence("discord", "d-a", "u3", present=True, is_bot=False)
    await coord.on_voice_presence("stoat", "s-a", "u4", present=True, is_bot=False)
    assert coord.active_group == "b"

    await coord.on_voice_presence("discord", "d-b", "u1", present=False, is_bot=False)
    await coord.on_voice_presence("stoat", "s-b", "u2", present=False, is_bot=False)
    assert coord.active_group is None


async def test_follow_on_empty_picks_another_already_qualified_group(fake_db):
    mappings = ChannelMappingRepository(fake_db)
    await _link(mappings, "a", "discord", "d-a")
    await _link(mappings, "a", "stoat", "s-a")
    await _link(mappings, "b", "discord", "d-b")
    await _link(mappings, "b", "stoat", "s-b")
    connectors = {
        "discord": make_connector("discord", voice_channels={"d-a", "d-b"}),
        "stoat": make_connector("stoat", voice_channels={"s-a", "s-b"}),
    }
    coord = VoiceBridgeCoordinator(mappings, connectors, follow_on_empty=True)
    await coord.refresh_groups()
    await coord.on_voice_presence("discord", "d-b", "u1", present=True, is_bot=False)
    await coord.on_voice_presence("stoat", "s-b", "u2", present=True, is_bot=False)
    await coord.on_voice_presence("discord", "d-a", "u3", present=True, is_bot=False)
    await coord.on_voice_presence("stoat", "s-a", "u4", present=True, is_bot=False)
    assert coord.active_group == "b"

    await coord.on_voice_presence("discord", "d-b", "u1", present=False, is_bot=False)
    await coord.on_voice_presence("stoat", "s-b", "u2", present=False, is_bot=False)
    assert coord.active_group == "a"


async def test_presence_on_unlinked_channel_is_ignored(fake_db):
    mappings = ChannelMappingRepository(fake_db)
    connectors = {"discord": make_connector("discord", voice_channels={"d-vc"})}
    coord = VoiceBridgeCoordinator(mappings, connectors)
    await coord.refresh_groups()
    await coord.on_voice_presence("discord", "d-vc", "u1", present=True, is_bot=False)
    assert coord.active_group is None
    assert coord.occupants("g") == {}


async def test_unlinking_a_voice_bridgeable_group_closes_its_session(fake_db):
    mappings = ChannelMappingRepository(fake_db)
    await _link(mappings, "g", "discord", "d-vc")
    await _link(mappings, "g", "stoat", "s-vc")
    connectors = {
        "discord": make_connector("discord", voice_channels={"d-vc"}),
        "stoat": make_connector("stoat", voice_channels={"s-vc"}),
    }
    coord = VoiceBridgeCoordinator(mappings, connectors)
    await coord.refresh_groups()
    await coord.on_voice_presence("discord", "d-vc", "u1", present=True, is_bot=False)
    await coord.on_voice_presence("stoat", "s-vc", "u2", present=True, is_bot=False)
    assert coord.active_group == "g"

    await mappings.delete_group("g")
    await coord.refresh_groups()
    assert coord.active_group is None
    assert coord.voice_bridgeable_groups() == {}
