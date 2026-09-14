"""Shared voice-bridging types.

`OnVoicePresence` is the one callback every voice-capable sender calls into
`VoiceBridgeCoordinator` with, from its own connector's voice-state events
(Discord's `on_voice_state_update`, Stoat's `voice_channel_join` /
`voice_channel_leave` / `voice_channel_move`). `VoiceConnector` / `VoiceTransport`
(the actual join/audio protocols) land in a later phase - this module only
carries what Phase 1's classification-and-presence work needs.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

# (connector_id, channel_id, user_id, *, present, is_bot) - a user joined
# (`present=True`) or left (`present=False`) a voice channel on `connector_id`.
# `is_bot` lets `VoiceBridgeCoordinator` exclude bot occupants (including the
# bridge's own voice connections, once Phase 2 makes any) from presence counts.
OnVoicePresence = Callable[..., Awaitable[None]]
