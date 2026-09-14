"""Stoat's `VoiceConnector`/`VoiceTransport` (issue #113 Phase 2) - joins a
Stoat voice channel and holds the resulting `livekit.rtc.Room`. Send/receive
audio (Phase 3) publishes/subscribes tracks on top of this same `Room` -
`start`/`set_output` stay `VoiceTransport`'s Phase 3 no-ops here.

stoat.py's own `VoiceChannel.connect()` (`stoat.abc.Connectable.connect`)
already lazily imports `livekit.rtc` and raises `TypeError` if it isn't
installed, so this module doesn't need its own hard dependency on `livekit`
either - `voice_available` just does a cheap `importlib` probe (no import)
so the coordinator can skip a connector it already knows can't join, without
this module needing a new project dependency to check that."""

from __future__ import annotations

import importlib.util
import logging
from typing import TYPE_CHECKING

from stoat_discord_bridge.services.voice.base import VoiceJoinError, VoiceTransport

if TYPE_CHECKING:
    import stoat

logger = logging.getLogger(__name__)


def _livekit_importable() -> bool:
    return importlib.util.find_spec("livekit.rtc") is not None


class StoatVoiceTransport(VoiceTransport):
    def __init__(self, connector_id: str, room: object) -> None:
        self.connector_id = connector_id
        self._room = room

    async def close(self) -> None:
        await self._room.disconnect()


class StoatVoiceConnector:
    """Owns one Stoat connector's voice-join capability -
    `VoiceBridgeCoordinator` holds one of these per Stoat connector. Channel
    resolution is cache-only (`client.get_channel(..., partial=False)`),
    matching `channel_is_voice`/`voice_occupants`'s own lookup pattern."""

    def __init__(
        self,
        connector_id: str,
        client: "stoat.Client",
        *,
        voice_bridging: bool,
        voice_node: str | None = None,
    ) -> None:
        self.connector_id = connector_id
        self._client = client
        self._voice_bridging = voice_bridging
        self._voice_node = voice_node

    @property
    def voice_available(self) -> bool:
        return self._voice_bridging and _livekit_importable()

    async def join(self, channel_id: str) -> StoatVoiceTransport:
        channel = self._client.get_channel(channel_id, partial=False)
        if channel is None:
            raise VoiceJoinError(f"Stoat voice channel {channel_id!r} not found on connector {self.connector_id!r}")
        try:
            room = await channel.connect(node=self._voice_node)
        except Exception as exc:
            raise VoiceJoinError(
                f"failed to join Stoat voice channel {channel_id!r} on connector {self.connector_id!r}: {exc}"
            ) from exc
        return StoatVoiceTransport(self.connector_id, room)
