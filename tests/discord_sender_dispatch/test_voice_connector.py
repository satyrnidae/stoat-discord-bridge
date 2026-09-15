"""`DiscordVoiceConnector`/`DiscordVoiceTransport` (issue #113) - real
join/close (Phase 2) and real send/receive wiring (Phase 3) against
`FakeClient`/`FakeVoiceChannel`/`FakeDiscordVoiceClient`, no real discord.py
network/voice involved. `discord.ext.voice_recv` and `discord.opus` ARE the
real installed libraries here (the `voice` extra, `pyproject.toml`) - only
`discord.opus.is_loaded()` needs monkeypatching, since this test environment
has no native libopus for discord.py to find.
"""

from __future__ import annotations

import asyncio

import discord
import pytest
from discord.ext import voice_recv

from stoat_discord_bridge.services.discord_service.voice import BridgeAudioSource, DiscordVoiceConnector
from stoat_discord_bridge.services.voice import pipeline
from stoat_discord_bridge.services.voice.base import VoiceJoinError
from tests.fakes.fake_discord import FakeChannel, FakeClient, FakeDiscordVoiceClient, FakeUser, FakeVoiceChannel


def _all_voice_available(monkeypatch, *, nacl: bool = True, opus: bool = True) -> None:
    import discord.voice_client as discord_voice_client

    monkeypatch.setattr(discord_voice_client, "has_nacl", nacl)
    monkeypatch.setattr(discord.opus, "is_loaded", lambda: opus)


def test_voice_available_true_when_bridging_on_nacl_and_opus_present(monkeypatch):
    _all_voice_available(monkeypatch)
    connector = DiscordVoiceConnector("discord", FakeClient(), voice_bridging=True)
    assert connector.voice_available is True


def test_voice_available_false_when_voice_bridging_off(monkeypatch):
    _all_voice_available(monkeypatch)
    connector = DiscordVoiceConnector("discord", FakeClient(), voice_bridging=False)
    assert connector.voice_available is False


def test_voice_available_false_when_nacl_missing(monkeypatch):
    _all_voice_available(monkeypatch, nacl=False)
    connector = DiscordVoiceConnector("discord", FakeClient(), voice_bridging=True)
    assert connector.voice_available is False


def test_voice_available_false_when_opus_not_loaded(monkeypatch):
    _all_voice_available(monkeypatch, opus=False)
    connector = DiscordVoiceConnector("discord", FakeClient(), voice_bridging=True)
    assert connector.voice_available is False


def test_voice_available_false_when_voice_recv_not_importable(monkeypatch):
    import stoat_discord_bridge.services.discord_service.voice as voice_module

    _all_voice_available(monkeypatch)
    monkeypatch.setattr(voice_module, "_voice_recv_importable", lambda: False)
    connector = DiscordVoiceConnector("discord", FakeClient(), voice_bridging=True)
    assert connector.voice_available is False


async def test_join_connects_and_returns_transport():
    client = FakeClient()
    voice_client = FakeDiscordVoiceClient()
    channel = client.add_channel(FakeVoiceChannel(1, connect_result=voice_client))
    connector = DiscordVoiceConnector("discord", client, voice_bridging=True)

    transport = await connector.join("1")

    assert transport.connector_id == "discord"
    assert channel.connect_calls == [voice_recv.VoiceRecvClient]


async def test_join_falls_back_to_fetch_on_cache_miss():
    """A channel `refresh_groups` classified as voice-bridkeable through
    `channel_is_voice`'s fetch-capable resolution must still be joinable
    even if it isn't in `get_channel`'s local cache yet - matching
    `channel_is_voice`/`voice_occupants`'s own get-or-fetch pattern."""

    class _FetchOnlyClient:
        def __init__(self, channel):
            self._channel = channel
            self.fetch_calls: list[int] = []

        def get_channel(self, channel_id):
            return None

        async def fetch_channel(self, channel_id):
            self.fetch_calls.append(channel_id)
            return self._channel

    voice_client = FakeDiscordVoiceClient()
    channel = FakeVoiceChannel(1, connect_result=voice_client)
    client = _FetchOnlyClient(channel)
    connector = DiscordVoiceConnector("discord", client, voice_bridging=True)

    transport = await connector.join("1")

    assert transport.connector_id == "discord"
    assert client.fetch_calls == [1]


async def test_join_unknown_channel_raises_voice_join_error():
    connector = DiscordVoiceConnector("discord", FakeClient(), voice_bridging=True)
    with pytest.raises(VoiceJoinError):
        await connector.join("999")


async def test_join_non_voice_channel_raises_voice_join_error():
    client = FakeClient()
    client.add_channel(FakeChannel(1, name="text"))
    connector = DiscordVoiceConnector("discord", client, voice_bridging=True)
    with pytest.raises(VoiceJoinError):
        await connector.join("1")


async def test_join_connect_failure_raises_voice_join_error():
    client = FakeClient()
    client.add_channel(FakeVoiceChannel(1, connect_error=RuntimeError("PyNaCl library needed in order to use voice")))
    connector = DiscordVoiceConnector("discord", client, voice_bridging=True)
    with pytest.raises(VoiceJoinError):
        await connector.join("1")


async def test_transport_close_disconnects_if_still_connected():
    voice_client = FakeDiscordVoiceClient()
    client = FakeClient()
    client.add_channel(FakeVoiceChannel(1, connect_result=voice_client))
    connector = DiscordVoiceConnector("discord", client, voice_bridging=True)
    transport = await connector.join("1")

    await transport.close()

    assert voice_client.disconnect_calls == [False]


async def test_transport_close_is_noop_if_already_disconnected():
    voice_client = FakeDiscordVoiceClient()
    voice_client.connected = False
    client = FakeClient()
    client.add_channel(FakeVoiceChannel(1, connect_result=voice_client))
    connector = DiscordVoiceConnector("discord", client, voice_bridging=True)
    transport = await connector.join("1")

    await transport.close()

    assert voice_client.disconnect_calls == []


# ---------------------------------------------------------------- Phase 3: start/set_output


async def _joined_transport():
    voice_client = FakeDiscordVoiceClient()
    client = FakeClient()
    client.add_channel(FakeVoiceChannel(1, connect_result=voice_client))
    connector = DiscordVoiceConnector("discord", client, voice_bridging=True)
    transport = await connector.join("1")
    return transport, voice_client


async def test_start_attaches_a_sink_that_routes_non_bot_speaker_frames():
    from types import SimpleNamespace

    transport, voice_client = await _joined_transport()
    calls: list = []

    async def on_speaker_frame(connector_id, user_id, frame):
        calls.append((connector_id, user_id, frame))

    await transport.start(on_speaker_frame)
    assert len(voice_client.listen_calls) == 1
    sink = voice_client.listen_calls[0]
    assert sink.wants_opus() is False

    sink.write(FakeUser(id=7, bot=False), SimpleNamespace(pcm=b"\x01\x02"))
    # run_coroutine_threadsafe needs two round-trips through the loop: one
    # for call_soon_threadsafe's callback to schedule the coroutine as a
    # task, another for that task to actually run.
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert calls == [("discord", "7", b"\x01\x02")]


async def test_start_sink_skips_bots_and_unresolved_speakers():
    from types import SimpleNamespace

    transport, voice_client = await _joined_transport()
    calls: list = []

    async def on_speaker_frame(connector_id, user_id, frame):
        calls.append((connector_id, user_id, frame))

    await transport.start(on_speaker_frame)
    sink = voice_client.listen_calls[0]

    sink.write(FakeUser(id=1, bot=True), SimpleNamespace(pcm=b"\x01"))
    sink.write(None, SimpleNamespace(pcm=b"\x02"))
    await asyncio.sleep(0)

    assert calls == []


async def test_listen_restarts_after_an_unexpected_stop():
    """`discord-ext-voice-recv` 0.5.2a179's packet router treats any single
    packet-decode exception (an ordinary corrupted Opus frame, not a bug of
    ours) as fatal and calls `stop_listening()` - without a restart, one bad
    packet would permanently end audio receive for the rest of the voice
    session."""
    transport, voice_client = await _joined_transport()
    await transport.start(lambda *a, **k: None)
    first_sink = voice_client.listen_calls[0]
    assert voice_client.is_listening() is True

    voice_client.simulate_listen_stopped(RuntimeError("corrupted stream"))
    await asyncio.sleep(0)  # let call_soon_threadsafe's callback run

    assert voice_client.is_listening() is True
    assert voice_client.listen_calls == [first_sink, first_sink]


async def test_listen_gives_up_restarting_after_repeated_crashes(monkeypatch):
    """A persistent (not one-off) failure must degrade to silence rather
    than a tight crash-restart loop - each restart interrupts an in-flight
    decode, and enough of those in quick succession is itself audible as
    continuous garbled noise, which is worse than just going quiet."""
    import stoat_discord_bridge.services.discord_service.voice as voice_mod

    monkeypatch.setattr(voice_mod, "_RESTART_MAX_IN_WINDOW", 2)
    transport, voice_client = await _joined_transport()
    await transport.start(lambda *a, **k: None)

    voice_client.simulate_listen_stopped(RuntimeError("corrupted stream"))
    await asyncio.sleep(0)
    voice_client.simulate_listen_stopped(RuntimeError("corrupted stream"))
    await asyncio.sleep(0)
    assert voice_client.is_listening() is True
    assert len(voice_client.listen_calls) == 3  # initial + 2 allowed restarts

    voice_client.simulate_listen_stopped(RuntimeError("corrupted stream"))
    await asyncio.sleep(0)

    assert voice_client.is_listening() is False
    assert len(voice_client.listen_calls) == 3  # the 3rd restart was refused


async def test_listen_does_not_restart_after_a_deliberate_stop():
    transport, voice_client = await _joined_transport()
    await transport.start(lambda *a, **k: None)

    await transport.close()
    await asyncio.sleep(0)

    assert voice_client.is_listening() is False
    assert len(voice_client.listen_calls) == 1


async def test_listen_does_not_restart_on_a_clean_stop_with_no_error():
    transport, voice_client = await _joined_transport()
    await transport.start(lambda *a, **k: None)

    voice_client.simulate_listen_stopped(None)
    await asyncio.sleep(0)

    assert voice_client.is_listening() is False
    assert len(voice_client.listen_calls) == 1


async def test_set_output_plays_a_bridge_audio_source_wrapping_the_holder():
    transport, voice_client = await _joined_transport()
    holder = pipeline.LatestFrameHolder()
    holder.set(b"\x09" * pipeline.FRAME_BYTES)

    transport.set_output(holder)

    assert len(voice_client.play_calls) == 1
    source = voice_client.play_calls[0]
    assert isinstance(source, BridgeAudioSource)
    assert source.read() == b"\x09" * pipeline.FRAME_BYTES
    assert source.is_opus() is False


async def test_transport_close_stops_listening_and_playing_if_active():
    transport, voice_client = await _joined_transport()
    await transport.start(lambda *a, **k: None)
    transport.set_output(pipeline.LatestFrameHolder())

    await transport.close()

    assert voice_client.stop_listening_calls == 1
    assert voice_client.stop_calls == 1
    assert voice_client.disconnect_calls == [False]


async def test_transport_close_skips_listening_and_playing_stops_if_inactive():
    transport, voice_client = await _joined_transport()

    await transport.close()

    assert voice_client.stop_listening_calls == 0
    assert voice_client.stop_calls == 0
    assert voice_client.disconnect_calls == [False]
