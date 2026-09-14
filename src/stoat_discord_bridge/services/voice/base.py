"""Shared voice-bridging types.

`OnVoicePresence` is the one callback every voice-capable sender calls into
`VoiceBridgeCoordinator` with, from its own connector's voice-state events
(Discord's `on_voice_state_update`, Stoat's `voice_channel_join` /
`voice_channel_leave` / `voice_channel_move`).

`VoiceConnector` / `VoiceTransport` (issue #113 Phase 2) are the actual
join/leave protocols - one `VoiceConnector` per voice-capable connector,
implemented by `DiscordVoiceConnector` / `StoatVoiceConnector`.
`VoiceBridgeCoordinator` talks to connectors only through these two types,
never to discord.py/stoat.py directly. Their audio surface
(`VoiceTransport.start`/`set_output`) is Phase 3 - stubbed as no-ops here so
the join/leave lifecycle (this phase) doesn't change shape once audio lands
on top of it.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from typing import Protocol, runtime_checkable

# (connector_id, channel_id, user_id, *, present, is_bot) - a user joined
# (`present=True`) or left (`present=False`) a voice channel on `connector_id`.
# `is_bot` lets `VoiceBridgeCoordinator` exclude bot occupants (including the
# bridge's own voice connections, once Phase 2 makes any) from presence counts.
OnVoicePresence = Callable[..., Awaitable[None]]

# (connector_id) - that connector's sender lost its native gateway connection
# (issue #113 Phase 2 resilience) - wired to
# `VoiceBridgeCoordinator.connector_disconnected`, which drops its stale
# presence/transport rather than waiting for the next periodic refresh.
OnVoiceConnectorLost = Callable[[str], Awaitable[None]]

# (speaker_connector_id, speaker_user_id, pcm_frame) - one 20ms frame of a
# remote speaker's audio, delivered by `VoiceTransport.start`. Phase 3 only;
# no transport calls this yet.
SpeakerFrameCallback = Callable[..., Awaitable[None]]


class VoiceJoinError(Exception):
    """Raised by `VoiceConnector.join` when a connector can't join a voice
    channel right now - the channel can't be resolved, the native voice
    dependency (PyNaCl / livekit) is missing, or the underlying connect call
    itself failed. A raising `join` has not created a transport; there is
    nothing for the caller to close."""


@runtime_checkable
class VoiceConnector(Protocol):
    """One per configured Discord/Stoat connector - owns that connector's
    native voice-join capability. `VoiceBridgeCoordinator` holds one of
    these per voice-capable connector and calls `join` against it to open
    or reconcile a session; it never talks to discord.py/stoat.py directly."""

    connector_id: str

    @property
    def voice_available(self) -> bool:
        """Whether this connector can currently join voice at all - the
        native voice dependency is present and this connector's own
        `voice_bridging` config is on. Doesn't guarantee a specific `join`
        call will succeed (auth, permissions, a bad channel id, etc. are
        still possible) - that's what `VoiceJoinError` is for."""
        ...

    async def join(self, channel_id: str) -> "VoiceTransport":
        """Join `channel_id`'s voice call and return the live transport.
        Raises `VoiceJoinError` on failure."""
        ...


class VoiceTransport(ABC):
    """A live voice connection to one connector's voice channel, returned by
    `VoiceConnector.join`. `start`/`set_output` are Phase 3's audio surface -
    no-ops until then; this phase only ever calls `close`."""

    connector_id: str

    async def start(self, on_speaker_frame: SpeakerFrameCallback) -> None:
        """Begin delivering `(speaker_id, pcm_frame)` for every remote
        speaker via `on_speaker_frame`. No-op until Phase 3."""
        return None

    def set_output(self, source: object) -> None:
        """Set what this connector's own voice channel should play. No-op
        until Phase 3."""
        return None

    @abstractmethod
    async def close(self) -> None:
        """Disconnect from the voice channel."""
