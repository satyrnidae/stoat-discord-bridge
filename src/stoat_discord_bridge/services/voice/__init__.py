"""Voice bridging (issue #113) - N-way live audio between a bridge group's
linked voice channels, on top of the same `/link channel` mapping ordinary
text relay uses. See `coordinator.py`'s `VoiceBridgeCoordinator` module
docstring for the design; `pipeline.py` and real audio send/receive land in
a later phase.
"""

from __future__ import annotations

from stoat_discord_bridge.services.voice.base import (
    OnVoiceConnectorLost,
    OnVoicePresence,
    VoiceConnector,
    VoiceJoinError,
    VoiceTransport,
)
from stoat_discord_bridge.services.voice.coordinator import VoiceBridgeCoordinator

__all__ = [
    "OnVoiceConnectorLost",
    "OnVoicePresence",
    "VoiceBridgeCoordinator",
    "VoiceConnector",
    "VoiceJoinError",
    "VoiceTransport",
]
