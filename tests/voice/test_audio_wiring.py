"""VoiceBridgeCoordinator wiring the audio pipeline onto real joins (issue
#113 Phase 3): `_wire_audio` calling `start`/`set_output` on every
real-transport join, a pushed speaker frame actually reaching every *other*
joined connector's mix (not its own - the "no loopback" invariant, exercised
end to end here rather than just at `MixSource`'s own unit level), and the
mixer clock's lifecycle tracking session open/close.
"""

from __future__ import annotations

from stoat_discord_bridge.services.voice import pipeline
from stoat_discord_bridge.services.voice.coordinator import VoiceBridgeCoordinator
from tests.voice.test_join_leave import _link, _two_connector_group


async def test_opening_session_wires_start_and_set_output_on_every_transport(fake_db):
    coord, _, voice_connectors = await _two_connector_group(fake_db)

    await coord.refresh_groups()

    transport_d = voice_connectors["discord"].transports[0]
    transport_s = voice_connectors["stoat"].transports[0]
    assert len(transport_d.start_calls) == 1
    assert len(transport_d.set_output_calls) == 1
    assert len(transport_s.start_calls) == 1
    assert len(transport_s.set_output_calls) == 1


async def test_speaker_frame_reaches_other_connectors_mix_but_not_its_own(fake_db):
    coord, _, voice_connectors = await _two_connector_group(fake_db)
    await coord.refresh_groups()
    transport_d = voice_connectors["discord"].transports[0]
    transport_s = voice_connectors["stoat"].transports[0]
    on_speaker_frame_from_discord = transport_d.start_calls[0]
    discord_holder = transport_d.set_output_calls[0]
    stoat_holder = transport_s.set_output_calls[0]

    frame = b"\x05" * pipeline.FRAME_BYTES
    await on_speaker_frame_from_discord("discord", "u1", frame)
    coord._mixer_clock.tick_once()

    assert stoat_holder.read() == frame
    assert discord_holder.read() == pipeline.SILENCE_FRAME


async def test_closing_session_tears_down_the_mixer_clock(fake_db):
    coord, connector_infos, voice_connectors = await _two_connector_group(fake_db)
    await coord.refresh_groups()
    assert coord._mixer_clock is not None

    from tests.voice.conftest import make_connector

    connector_infos["discord"] = make_connector("discord", voice_channels={"d-vc"}, occupants={"d-vc": set()})
    await coord.on_voice_presence("discord", "d-vc", "u1", present=False, is_bot=False)

    assert coord.active_group is None
    assert coord._mixer_clock is None


async def test_third_connector_joining_mid_session_shares_the_same_mixer_clock(fake_db):
    from stoat_discord_bridge.storage.channel_mappings import ChannelMappingRepository
    from tests.voice.conftest import FakeVoiceConnector, make_connector

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
    mixer_clock = coord._mixer_clock

    await coord.on_voice_presence("stoat_sh", "sh-vc", "u3", present=True, is_bot=False)

    assert coord._mixer_clock is mixer_clock  # same clock reused, not recreated
    transport_sh = voice_connectors["stoat_sh"].transports[0]
    assert len(transport_sh.start_calls) == 1
    assert len(transport_sh.set_output_calls) == 1
