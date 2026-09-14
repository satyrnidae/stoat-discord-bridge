"""Voice bridging (issue #113) - N-way live audio between a bridge group's
linked voice channels, on top of the same `/link channel` mapping ordinary
text relay uses. See `coordinator.py`'s `VoiceBridgeCoordinator` module
docstring for the design; `pipeline.py` and the per-connector transports land
in a later phase.
"""

from __future__ import annotations

from stoat_discord_bridge.services.voice.base import OnVoicePresence
from stoat_discord_bridge.services.voice.coordinator import VoiceBridgeCoordinator

__all__ = ["OnVoicePresence", "VoiceBridgeCoordinator"]
