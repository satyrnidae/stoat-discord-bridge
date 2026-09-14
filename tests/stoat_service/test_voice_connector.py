"""`StoatVoiceConnector`/`StoatVoiceTransport` (issue #113 Phase 2) - real
join/close against `FakeClient`/`FakeVoiceChannel`/`FakeStoatRoom`, no real
stoat.py network/livekit involved.
"""

from __future__ import annotations

import pytest

from stoat_discord_bridge.services.stoat_service.voice import StoatVoiceConnector
from stoat_discord_bridge.services.voice.base import VoiceJoinError
from tests.fakes.fake_stoat import FakeChannel, FakeClient, FakeStoatRoom, FakeVoiceChannel


def test_voice_available_true_when_bridging_on_and_livekit_importable(monkeypatch):
    import stoat_discord_bridge.services.stoat_service.voice as voice_mod

    monkeypatch.setattr(voice_mod, "_livekit_importable", lambda: True)
    connector = StoatVoiceConnector("stoat", FakeClient(), voice_bridging=True)
    assert connector.voice_available is True


def test_voice_available_false_when_voice_bridging_off(monkeypatch):
    import stoat_discord_bridge.services.stoat_service.voice as voice_mod

    monkeypatch.setattr(voice_mod, "_livekit_importable", lambda: True)
    connector = StoatVoiceConnector("stoat", FakeClient(), voice_bridging=False)
    assert connector.voice_available is False


def test_voice_available_false_when_livekit_not_importable(monkeypatch):
    import stoat_discord_bridge.services.stoat_service.voice as voice_mod

    monkeypatch.setattr(voice_mod, "_livekit_importable", lambda: False)
    connector = StoatVoiceConnector("stoat", FakeClient(), voice_bridging=True)
    assert connector.voice_available is False


async def test_join_connects_and_returns_transport():
    client = FakeClient()
    room = FakeStoatRoom()
    channel = client.add_channel(FakeVoiceChannel("c1", connect_result=room))
    connector = StoatVoiceConnector("stoat", client, voice_bridging=True, voice_node="worldwide")

    transport = await connector.join("c1")

    assert transport.connector_id == "stoat"
    assert channel.connect_calls == ["worldwide"]


async def test_join_unknown_channel_raises_voice_join_error():
    connector = StoatVoiceConnector("stoat", FakeClient(), voice_bridging=True)
    with pytest.raises(VoiceJoinError):
        await connector.join("missing")


async def test_join_connect_failure_raises_voice_join_error():
    client = FakeClient()
    client.add_channel(FakeVoiceChannel("c1", connect_error=TypeError("Livekit is unavailable")))
    connector = StoatVoiceConnector("stoat", client, voice_bridging=True)
    with pytest.raises(VoiceJoinError):
        await connector.join("c1")


async def test_join_non_voice_channel_that_raises_on_connect_wraps_as_voice_join_error():
    """Unlike Discord, a Stoat TextChannel technically has a `connect()`
    method too (stoat.abc.Connectable) - the server itself rejects joining
    one (CannotJoinCall), so this connector doesn't isinstance-check the
    channel, it just relies on the (wrapped) HTTPException from a bad
    connect() call, exercised here via a plain non-voice FakeChannel with no
    connect() at all raising AttributeError."""
    client = FakeClient()
    client.add_channel(FakeChannel("c1", name="text"))
    connector = StoatVoiceConnector("stoat", client, voice_bridging=True)
    with pytest.raises(VoiceJoinError):
        await connector.join("c1")


async def test_transport_close_disconnects_room():
    room = FakeStoatRoom()
    client = FakeClient()
    client.add_channel(FakeVoiceChannel("c1", connect_result=room))
    connector = StoatVoiceConnector("stoat", client, voice_bridging=True)
    transport = await connector.join("c1")

    await transport.close()

    assert room.disconnect_calls == 1
