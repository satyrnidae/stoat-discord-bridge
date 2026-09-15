"""VoiceBridgeCoordinator's Phase 2 join/leave orchestration: `_open_session`
/ `_reconcile_membership` / `_close_session` actually calling
`VoiceConnector.join`/`VoiceTransport.close` (via fakes - no real
discord.py/stoat.py/livekit involved), rather than Phase 1's logging no-ops.
"""

from __future__ import annotations

from stoat_discord_bridge.services.voice.coordinator import VoiceBridgeCoordinator
from stoat_discord_bridge.storage.channel_mappings import ChannelMapping, ChannelMappingRepository
from tests.voice.conftest import FakeVoiceConnector, make_connector


async def _link(mappings: ChannelMappingRepository, group: str, connector_id: str, channel_id: str) -> None:
    await mappings.upsert(
        ChannelMapping(bridge_group=group, connector_id=connector_id, channel_id=channel_id, channel_name=channel_id)
    )


async def _two_connector_group(
    fake_db,
    *,
    occupants: dict[str, set[str]] | None = None,
    voice_connectors: dict[str, FakeVoiceConnector] | None = None,
):
    """A single voice-bridgeable group "g" linking discord's "d-vc" and
    stoat's "s-vc", with fake voice connectors for both. Returns
    (coordinator, connector_infos, voice_connectors)."""
    occupants = occupants or {"d-vc": {"u1"}, "s-vc": {"u2"}}
    mappings = ChannelMappingRepository(fake_db)
    await _link(mappings, "g", "discord", "d-vc")
    await _link(mappings, "g", "stoat", "s-vc")
    connector_infos = {
        "discord": make_connector("discord", voice_channels={"d-vc"}, occupants={"d-vc": occupants.get("d-vc")}),
        "stoat": make_connector("stoat", voice_channels={"s-vc"}, occupants={"s-vc": occupants.get("s-vc")}),
    }
    voice_connectors = voice_connectors or {
        "discord": FakeVoiceConnector("discord"),
        "stoat": FakeVoiceConnector("stoat"),
    }
    coord = VoiceBridgeCoordinator(mappings, connector_infos, voice_connectors=voice_connectors)
    return coord, connector_infos, voice_connectors


async def test_voice_connectors_dict_populated_after_construction_is_still_seen(fake_db):
    """`bridge.py` constructs `voice_connectors` as an *empty* dict, hands it
    to `VoiceBridgeCoordinator.__init__`, and only populates it afterward as
    each sender is built - relying on the coordinator holding onto that same
    dict object rather than copying it. `voice_connectors or {}` would
    silently break that aliasing (an empty dict is falsy), permanently
    stranding the coordinator on a disconnected, forever-empty dict - every
    connector then hits the Phase 1 "no VoiceConnector wired" fallback
    (trivially "joined" with no real transport) instead of ever really
    joining, with no error anywhere to show for it."""
    mappings = ChannelMappingRepository(fake_db)
    await _link(mappings, "g", "discord", "d-vc")
    await _link(mappings, "g", "stoat", "s-vc")
    connector_infos = {
        "discord": make_connector("discord", voice_channels={"d-vc"}, occupants={"d-vc": {"u1"}}),
        "stoat": make_connector("stoat", voice_channels={"s-vc"}, occupants={"s-vc": {"u2"}}),
    }
    voice_connectors: dict[str, FakeVoiceConnector] = {}
    coord = VoiceBridgeCoordinator(mappings, connector_infos, voice_connectors=voice_connectors)
    # Mutated only *after* construction, matching bridge.py's real ordering.
    voice_connectors["discord"] = FakeVoiceConnector("discord")
    voice_connectors["stoat"] = FakeVoiceConnector("stoat")

    await coord.refresh_groups()

    assert voice_connectors["discord"].joined == ["d-vc"]
    assert voice_connectors["stoat"].joined == ["s-vc"]


async def test_opening_session_joins_every_populated_connector(fake_db):
    coord, _, voice_connectors = await _two_connector_group(fake_db)
    await coord.refresh_groups()
    assert coord.active_group == "g"
    assert coord.active_connectors == frozenset({"discord", "stoat"})
    assert voice_connectors["discord"].joined == ["d-vc"]
    assert voice_connectors["stoat"].joined == ["s-vc"]


async def test_closing_session_closes_every_transport(fake_db):
    coord, connector_infos, voice_connectors = await _two_connector_group(fake_db)
    await coord.refresh_groups()
    transport_d = voice_connectors["discord"].transports[0]
    transport_s = voice_connectors["stoat"].transports[0]

    # Discord's occupant empties -> only 1 populated connector left -> close.
    connector_infos["discord"] = make_connector("discord", voice_channels={"d-vc"}, occupants={"d-vc": set()})
    await coord.on_voice_presence("discord", "d-vc", "u1", present=False, is_bot=False)

    assert coord.active_group is None
    assert coord.active_connectors == frozenset()
    assert transport_d.closed
    assert transport_s.closed


async def test_connector_not_voice_available_is_skipped_not_joined(fake_db):
    voice_connectors = {
        "discord": FakeVoiceConnector("discord"),
        "stoat": FakeVoiceConnector("stoat", available=False),
    }
    coord, _, voice_connectors = await _two_connector_group(fake_db, voice_connectors=voice_connectors)
    await coord.refresh_groups()
    # Only 1 connector is actually available/populated -> can't reach 2 -> no session.
    assert coord.active_group is None
    assert voice_connectors["discord"].joined == []
    assert voice_connectors["stoat"].joined == []


async def test_third_connector_populating_mid_session_gets_joined(fake_db):
    mappings = ChannelMappingRepository(fake_db)
    await _link(mappings, "g", "discord", "d-vc")
    await _link(mappings, "g", "stoat", "s-vc")
    await _link(mappings, "g", "stoat_sh", "sh-vc")
    connector_infos = {
        "discord": make_connector("discord", voice_channels={"d-vc"}, occupants={"d-vc": {"u1"}}),
        "stoat": make_connector("stoat", voice_channels={"s-vc"}, occupants={"s-vc": {"u2"}}),
        "stoat_sh": make_connector("stoat_sh", voice_channels={"sh-vc"}, occupants={"sh-vc": set()}),
    }
    voice_connectors = {
        "discord": FakeVoiceConnector("discord"),
        "stoat": FakeVoiceConnector("stoat"),
        "stoat_sh": FakeVoiceConnector("stoat_sh"),
    }
    coord = VoiceBridgeCoordinator(mappings, connector_infos, voice_connectors=voice_connectors)
    await coord.refresh_groups()
    assert coord.active_connectors == frozenset({"discord", "stoat"})
    assert voice_connectors["stoat_sh"].joined == []

    await coord.on_voice_presence("stoat_sh", "sh-vc", "u3", present=True, is_bot=False)

    assert coord.active_connectors == frozenset({"discord", "stoat", "stoat_sh"})
    assert voice_connectors["stoat_sh"].joined == ["sh-vc"]


async def test_one_connector_emptying_parts_it_but_keeps_session(fake_db):
    mappings = ChannelMappingRepository(fake_db)
    await _link(mappings, "g", "discord", "d-vc")
    await _link(mappings, "g", "stoat", "s-vc")
    await _link(mappings, "g", "stoat_sh", "sh-vc")
    connector_infos = {
        "discord": make_connector("discord", voice_channels={"d-vc"}, occupants={"d-vc": {"u1"}}),
        "stoat": make_connector("stoat", voice_channels={"s-vc"}, occupants={"s-vc": {"u2"}}),
        "stoat_sh": make_connector("stoat_sh", voice_channels={"sh-vc"}, occupants={"sh-vc": {"u3"}}),
    }
    voice_connectors = {
        "discord": FakeVoiceConnector("discord"),
        "stoat": FakeVoiceConnector("stoat"),
        "stoat_sh": FakeVoiceConnector("stoat_sh"),
    }
    coord = VoiceBridgeCoordinator(mappings, connector_infos, voice_connectors=voice_connectors)
    await coord.refresh_groups()
    assert coord.active_connectors == frozenset({"discord", "stoat", "stoat_sh"})
    transport_sh = voice_connectors["stoat_sh"].transports[0]

    await coord.on_voice_presence("stoat_sh", "sh-vc", "u3", present=False, is_bot=False)

    assert coord.active_group == "g"
    assert coord.active_connectors == frozenset({"discord", "stoat"})
    assert transport_sh.closed


async def test_partial_join_failure_still_opens_with_remaining_two(fake_db):
    mappings = ChannelMappingRepository(fake_db)
    await _link(mappings, "g", "discord", "d-vc")
    await _link(mappings, "g", "stoat", "s-vc")
    await _link(mappings, "g", "stoat_sh", "sh-vc")
    connector_infos = {
        "discord": make_connector("discord", voice_channels={"d-vc"}, occupants={"d-vc": {"u1"}}),
        "stoat": make_connector("stoat", voice_channels={"s-vc"}, occupants={"s-vc": {"u2"}}),
        "stoat_sh": make_connector("stoat_sh", voice_channels={"sh-vc"}, occupants={"sh-vc": {"u3"}}),
    }
    voice_connectors = {
        "discord": FakeVoiceConnector("discord"),
        "stoat": FakeVoiceConnector("stoat", fail=True),
        "stoat_sh": FakeVoiceConnector("stoat_sh"),
    }
    coord = VoiceBridgeCoordinator(mappings, connector_infos, voice_connectors=voice_connectors)
    await coord.refresh_groups()

    assert coord.active_group == "g"
    assert coord.active_connectors == frozenset({"discord", "stoat_sh"})
    assert voice_connectors["stoat"].joined == []


async def test_join_failures_leaving_fewer_than_two_rolls_back(fake_db):
    voice_connectors = {
        "discord": FakeVoiceConnector("discord"),
        "stoat": FakeVoiceConnector("stoat", fail=True),
    }
    coord, _, voice_connectors = await _two_connector_group(fake_db, voice_connectors=voice_connectors)

    await coord.refresh_groups()

    assert coord.active_group is None
    assert coord.active_connectors == frozenset()
    # The one connector that *did* join gets rolled back (closed) too.
    assert voice_connectors["discord"].transports[0].closed


async def test_rollback_falls_through_to_next_eligible_group(fake_db):
    mappings = ChannelMappingRepository(fake_db)
    await _link(mappings, "a", "discord", "d-vc")
    await _link(mappings, "a", "stoat", "s-vc")
    await _link(mappings, "b", "discord2", "d2-vc")
    await _link(mappings, "b", "stoat2", "s2-vc")
    connector_infos = {
        "discord": make_connector("discord", voice_channels={"d-vc"}, occupants={"d-vc": {"u1"}}),
        "stoat": make_connector("stoat", voice_channels={"s-vc"}, occupants={"s-vc": {"u2"}}),
        "discord2": make_connector("discord2", voice_channels={"d2-vc"}, occupants={"d2-vc": {"u3"}}),
        "stoat2": make_connector("stoat2", voice_channels={"s2-vc"}, occupants={"s2-vc": {"u4"}}),
    }
    voice_connectors = {
        "discord": FakeVoiceConnector("discord", fail=True),
        "stoat": FakeVoiceConnector("stoat", fail=True),
        "discord2": FakeVoiceConnector("discord2"),
        "stoat2": FakeVoiceConnector("stoat2"),
    }
    coord = VoiceBridgeCoordinator(mappings, connector_infos, voice_connectors=voice_connectors)

    await coord.refresh_groups()

    assert coord.active_group == "b"
    assert coord.active_connectors == frozenset({"discord2", "stoat2"})


async def test_no_voice_connectors_wired_falls_back_to_phase1_tracking_only(fake_db):
    """A deployment (or test) that hasn't wired any `VoiceConnector` at all -
    `voice_connectors` omitted entirely, matching Phase 1's constructor call
    - keeps Phase 1's exact behavior: active_group/active_connectors track
    intended membership, nothing is actually joined. This is what keeps
    every Phase 1 coordinator test (tests/voice/test_coordinator.py) passing
    unchanged."""
    mappings = ChannelMappingRepository(fake_db)
    await _link(mappings, "g", "discord", "d-vc")
    await _link(mappings, "g", "stoat", "s-vc")
    connector_infos = {
        "discord": make_connector("discord", voice_channels={"d-vc"}, occupants={"d-vc": {"u1"}}),
        "stoat": make_connector("stoat", voice_channels={"s-vc"}, occupants={"s-vc": {"u2"}}),
    }
    coord = VoiceBridgeCoordinator(mappings, connector_infos)
    await coord.refresh_groups()
    assert coord.active_group == "g"
    assert coord.active_connectors == frozenset({"discord", "stoat"})


async def test_failed_open_retries_on_next_reevaluate_without_a_new_eligibility_edge(fake_db):
    """A group's first open attempt rolling back (e.g. a transient join
    failure) must not permanently lock it out of the edge-triggered opening
    logic - the very next reevaluate (any trigger, not just a fresh
    below-then-above-2 transition) tries it again."""
    voice_connectors = {
        "discord": FakeVoiceConnector("discord"),
        "stoat": FakeVoiceConnector("stoat", fail=True),
    }
    coord, _, voice_connectors = await _two_connector_group(fake_db, voice_connectors=voice_connectors)
    await coord.refresh_groups()
    assert coord.active_group is None

    # "stoat" recovers - occupancy hasn't changed, just whether it can join.
    voice_connectors["stoat"]._fail = False
    await coord.refresh_groups()

    assert coord.active_group == "g"
    assert coord.active_connectors == frozenset({"discord", "stoat"})


async def test_connector_disconnected_drops_presence_and_closes_transport(fake_db):
    coord, _, voice_connectors = await _two_connector_group(fake_db)
    await coord.refresh_groups()
    transport_d = voice_connectors["discord"].transports[0]

    await coord.connector_disconnected("discord")

    assert coord.active_group is None
    assert transport_d.closed
    assert coord.occupants("g").get("discord", frozenset()) == frozenset()
