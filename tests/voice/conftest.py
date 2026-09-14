"""Fakes shared by services/voice/ tests - a bare ConnectorInfo builder for
channel_is_voice/voice_occupants, since those are the only two hooks
VoiceBridgeCoordinator reads."""

from __future__ import annotations

from stoat_discord_bridge.admin_commands import ConnectorInfo


def make_connector(
    connector_id: str,
    *,
    voice_channels: set[str] | None = None,
    occupants: dict[str, set[str] | None] | None = None,
    channel_is_voice_raises: bool = False,
    no_voice_hook: bool = False,
) -> ConnectorInfo:
    """A ConnectorInfo whose `channel_is_voice` reports True for every id in
    `voice_channels` (False for any other known-looking id), and whose
    `voice_occupants` looks up `occupants` by channel id (missing key -> empty
    set; explicit None value -> "can't tell right now", the same as a real
    hook's None return)."""
    voice_channels = voice_channels or set()
    occupants = occupants or {}

    async def channel_is_voice(channel_id: str) -> bool | None:
        if channel_is_voice_raises:
            raise RuntimeError("boom")
        return channel_id in voice_channels

    async def voice_occupants(channel_id: str) -> set[str] | None:
        if channel_id not in occupants:
            return set()
        return occupants[channel_id]

    return ConnectorInfo(
        id=connector_id,
        label=connector_id,
        channel_is_voice=None if no_voice_hook else channel_is_voice,
        voice_occupants=voice_occupants,
    )
