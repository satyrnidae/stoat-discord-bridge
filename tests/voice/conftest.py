"""Fakes shared by services/voice/ tests - a bare ConnectorInfo builder for
channel_is_voice/voice_occupants, since those are the only two hooks
VoiceBridgeCoordinator reads for classification/presence (Phase 1), plus a
fake VoiceConnector/VoiceTransport pair for the join/leave lifecycle
(Phase 2) - VoiceBridgeCoordinator never touches discord.py/stoat.py
directly, so these are all it needs to exercise the real join/close calls
without a live server."""

from __future__ import annotations

from stoat_discord_bridge.admin_commands import ConnectorInfo
from stoat_discord_bridge.services.voice.base import VoiceJoinError, VoiceTransport


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


class FakeVoiceTransport(VoiceTransport):
    """Records whether/how it was closed; never actually connects to
    anything."""

    def __init__(self, connector_id: str) -> None:
        self.connector_id = connector_id
        self.closed = False

    async def close(self) -> None:
        self.closed = True


class FakeVoiceConnector:
    """A `VoiceConnector` whose `join` either always succeeds (recording
    every channel id it was asked to join, in order, and every
    `FakeVoiceTransport` it handed back) or always raises `VoiceJoinError`
    (`fail=True`) - never both, since a connector isn't expected to flip
    from failing to succeeding mid-test; construct a fresh one to change
    behavior partway through."""

    def __init__(self, connector_id: str, *, available: bool = True, fail: bool = False) -> None:
        self.connector_id = connector_id
        self._available = available
        self._fail = fail
        self.joined: list[str] = []
        self.transports: list[FakeVoiceTransport] = []

    @property
    def voice_available(self) -> bool:
        return self._available

    async def join(self, channel_id: str) -> FakeVoiceTransport:
        if self._fail:
            raise VoiceJoinError(f"fake connector {self.connector_id} failed to join {channel_id}")
        self.joined.append(channel_id)
        transport = FakeVoiceTransport(self.connector_id)
        self.transports.append(transport)
        return transport
