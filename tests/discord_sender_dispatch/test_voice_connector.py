"""`DiscordVoiceConnector`/`DiscordVoiceTransport` (issue #113 Phase 2) -
real join/close against `FakeClient`/`FakeVoiceChannel`/`FakeDiscordVoiceClient`,
no real discord.py network/voice involved.
"""

from __future__ import annotations

import discord
import pytest

from stoat_discord_bridge.services.discord_service.voice import DiscordVoiceConnector
from stoat_discord_bridge.services.voice.base import VoiceJoinError
from tests.fakes.fake_discord import FakeChannel, FakeClient, FakeDiscordVoiceClient, FakeVoiceChannel


def test_voice_available_true_when_bridging_on_and_nacl_present(monkeypatch):
    import discord.voice_client as discord_voice_client

    monkeypatch.setattr(discord_voice_client, "has_nacl", True)
    connector = DiscordVoiceConnector("discord", FakeClient(), voice_bridging=True)
    assert connector.voice_available is True


def test_voice_available_false_when_voice_bridging_off(monkeypatch):
    import discord.voice_client as discord_voice_client

    monkeypatch.setattr(discord_voice_client, "has_nacl", True)
    connector = DiscordVoiceConnector("discord", FakeClient(), voice_bridging=False)
    assert connector.voice_available is False


def test_voice_available_false_when_nacl_missing(monkeypatch):
    import discord.voice_client as discord_voice_client

    monkeypatch.setattr(discord_voice_client, "has_nacl", False)
    connector = DiscordVoiceConnector("discord", FakeClient(), voice_bridging=True)
    assert connector.voice_available is False


async def test_join_connects_and_returns_transport():
    client = FakeClient()
    voice_client = FakeDiscordVoiceClient()
    channel = client.add_channel(FakeVoiceChannel(1, connect_result=voice_client))
    connector = DiscordVoiceConnector("discord", client, voice_bridging=True)

    transport = await connector.join("1")

    assert transport.connector_id == "discord"
    assert channel.connect_calls == [discord.VoiceClient]


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
