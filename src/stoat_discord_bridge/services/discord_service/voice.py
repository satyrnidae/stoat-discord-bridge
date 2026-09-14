"""Discord's `VoiceConnector`/`VoiceTransport` (issue #113 Phase 2) - joins a
Discord voice channel and holds the resulting `discord.VoiceClient`. Send/
receive audio (Phase 3, via `discord-ext-voice-recv`) is added on top of this
without needing a new connection - `start`/`set_output` stay `VoiceTransport`'s
Phase 3 no-ops here.

Joining needs PyNaCl (the encrypted voice UDP transport) even for a silent
connection - discord.py already exposes whether it's installed as
`discord.voice_client.has_nacl` and raises `RuntimeError` from `connect()`
itself if it's missing, so this module doesn't need its own import probe or a
new project dependency; `voice_available` just surfaces that flag.
"""

from __future__ import annotations

import logging

import discord
from discord import voice_client as _discord_voice_client

from stoat_discord_bridge.services.voice.base import VoiceJoinError, VoiceTransport

logger = logging.getLogger(__name__)


class DiscordVoiceTransport(VoiceTransport):
    def __init__(self, connector_id: str, voice_client: "discord.VoiceClient") -> None:
        self.connector_id = connector_id
        self._voice_client = voice_client

    async def close(self) -> None:
        if self._voice_client.is_connected():
            await self._voice_client.disconnect(force=False)


class DiscordVoiceConnector:
    """Owns one Discord connector's voice-join capability -
    `VoiceBridgeCoordinator` holds one of these per Discord connector.
    Channel resolution falls back to a fetch on a cache miss, matching
    `channel_is_voice`/`voice_occupants`'s own lookup pattern (`lookups/names.py`)
    - `refresh_groups` classifies a group as voice-bridgeable through that
    fetch-capable path, so joining must resolve the same channel `join`
    otherwise fails on, spuriously, for one that just isn't cached locally
    yet."""

    def __init__(self, connector_id: str, client: "discord.Client", *, voice_bridging: bool) -> None:
        self.connector_id = connector_id
        self._client = client
        self._voice_bridging = voice_bridging

    @property
    def voice_available(self) -> bool:
        return self._voice_bridging and _discord_voice_client.has_nacl

    async def _get_voice_channel(self, channel_id: str) -> "discord.VoiceChannel | None":
        try:
            channel = self._client.get_channel(int(channel_id)) or await self._client.fetch_channel(int(channel_id))
        except Exception:
            return None
        return channel if isinstance(channel, discord.VoiceChannel) else None

    async def join(self, channel_id: str) -> DiscordVoiceTransport:
        channel = await self._get_voice_channel(channel_id)
        if channel is None:
            raise VoiceJoinError(f"Discord voice channel {channel_id!r} not found on connector {self.connector_id!r}")
        try:
            voice_client = await channel.connect(cls=discord.VoiceClient)
        except Exception as exc:
            raise VoiceJoinError(
                f"failed to join Discord voice channel {channel_id!r} on connector {self.connector_id!r}: {exc}"
            ) from exc
        return DiscordVoiceTransport(self.connector_id, voice_client)
