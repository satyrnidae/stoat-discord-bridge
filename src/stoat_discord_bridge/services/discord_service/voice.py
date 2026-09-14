"""Discord's `VoiceConnector`/`VoiceTransport` (issue #113) - joins a Discord
voice channel and holds the resulting `discord.ext.voice_recv.VoiceRecvClient`
(a drop-in `discord.VoiceClient` subclass that adds audio *receive*, which
plain discord.py lacks). Phase 2 built the join/leave lifecycle; Phase 3 adds
real send/receive on top of the same connection - no new connect call.

Joining needs PyNaCl (the encrypted voice UDP transport) even for a silent
connection - discord.py already exposes whether it's installed as
`discord.voice_client.has_nacl` and raises `RuntimeError` from `connect()`
itself if it's missing, so this module doesn't need its own import probe for
that. Actually encoding/decoding audio additionally needs libopus
(`discord.opus.is_loaded()`, auto-probed by discord.py at import time via
`ctypes.util.find_library`) and the optional `discord-ext-voice-recv`
package (the `[voice]` extra, `pyproject.toml`) - `voice_available` checks
all of these so the coordinator skips a connector that can't actually carry
audio rather than joining and immediately failing to `play()`/`listen()`.

`discord-ext-voice-recv` stays an *optional* dependency at runtime (a
deployment can run text-only bridging with it absent) - `voice_recv` is
never imported at module scope, only lazily once `voice_available` has
already confirmed it's installed, via `_voice_recv_module()` below.
"""

from __future__ import annotations

import asyncio
import importlib.util
import logging
from typing import TYPE_CHECKING

import discord
from discord import voice_client as _discord_voice_client

from stoat_discord_bridge.services.voice import pipeline
from stoat_discord_bridge.services.voice.base import VoiceJoinError, VoiceTransport

if TYPE_CHECKING:
    from discord.ext import voice_recv

logger = logging.getLogger(__name__)

_sink_class: "type | None" = None


def _voice_recv_importable() -> bool:
    return importlib.util.find_spec("discord.ext.voice_recv") is not None


def _voice_recv_module() -> "voice_recv":
    from discord.ext import voice_recv as module

    return module


def _get_sink_class() -> "type":
    """Lazily define (once) and return the `AudioSink` subclass that routes
    received speaker audio into a session's `SpeakerRegistry` - deferred
    behind `_voice_recv_module()` so `discord.ext.voice_recv.AudioSink`
    (the base class) is only ever touched once the caller has already
    confirmed it's installed."""
    global _sink_class
    if _sink_class is not None:
        return _sink_class

    voice_recv = _voice_recv_module()

    class _DiscordSpeakerSink(voice_recv.AudioSink):
        """Routes every non-bot speaker's decoded PCM into `on_speaker_frame`.
        `write()` runs on discord.py's own dedicated receive thread, not the
        asyncio loop - it hops onto the loop via `run_coroutine_threadsafe`
        rather than calling the (async) callback directly, since that thread
        can't `await` anything itself."""

        def __init__(self, connector_id: str, loop: "asyncio.AbstractEventLoop", on_speaker_frame) -> None:
            super().__init__()
            self.connector_id = connector_id
            self._loop = loop
            self._on_speaker_frame = on_speaker_frame

        def wants_opus(self) -> bool:
            return False

        def write(self, user, data) -> None:
            # Bots (including the bridge's own other connectors, if ever
            # audible to each other via some future direct link) never
            # count as a speaker - same stance as presence (issue #113
            # assumption #6). An unresolved SSRC (user is None, before
            # discord.py has matched the packet to a member) is skipped too
            # rather than buffered under a fake identity.
            if user is None or user.bot:
                return
            # write() runs on discord.py's own receive thread, not the
            # asyncio loop - on_speaker_frame is async (SpeakerFrameCallback,
            # services/voice/base.py), so it has to be scheduled back onto
            # the loop rather than awaited here directly.
            future = asyncio.run_coroutine_threadsafe(
                self._on_speaker_frame(self.connector_id, str(user.id), data.pcm), self._loop
            )
            future.add_done_callback(self._log_speaker_frame_error)

        def _log_speaker_frame_error(self, future: "asyncio.Future") -> None:
            exc = future.exception() if not future.cancelled() else None
            if exc is not None:
                logger.error(
                    "[voice] on_speaker_frame callback raised for connector %s", self.connector_id, exc_info=exc
                )

        def cleanup(self) -> None:
            return None

    _sink_class = _DiscordSpeakerSink
    return _sink_class


class BridgeAudioSource(discord.AudioSource):
    """Plays whatever `holder` currently holds - fed every 20ms by the
    session's `MixerClock` (issue #113 Phase 3). `read()` runs on discord.py's
    own dedicated player thread; `LatestFrameHolder.read` is a plain
    attribute read, safe there without a lock."""

    def __init__(self, holder: "pipeline.LatestFrameHolder") -> None:
        self._holder = holder

    def read(self) -> bytes:
        return self._holder.read()

    def is_opus(self) -> bool:
        return False


class DiscordVoiceTransport(VoiceTransport):
    def __init__(self, connector_id: str, voice_client: "voice_recv.VoiceRecvClient") -> None:
        self.connector_id = connector_id
        self._voice_client = voice_client
        self._sink = None

    async def start(self, on_speaker_frame) -> None:
        """Begin receiving: attach an `AudioSink` that hands every non-bot
        speaker's PCM to `on_speaker_frame(connector_id, user_id, pcm)` (an
        async callback - `SpeakerFrameCallback`, `services/voice/base.py` -
        scheduled onto this loop via `run_coroutine_threadsafe` since the
        sink's own `write()` runs on discord.py's receive thread, not the
        loop). The sink is kept on `self._sink` - not just handed to
        `listen()` - since nothing guarantees `VoiceRecvClient` itself holds
        the sole strong reference; without this the sink (and its bound
        `_loop`/`_on_speaker_frame`) could be garbage-collected once this
        method returns, silently starving `write()` callbacks."""
        loop = asyncio.get_running_loop()
        sink_cls = _get_sink_class()
        self._sink = sink_cls(self.connector_id, loop, on_speaker_frame)
        self._voice_client.listen(self._sink)

    def set_output(self, source: "pipeline.LatestFrameHolder") -> None:
        self._voice_client.play(BridgeAudioSource(source))

    async def close(self) -> None:
        if self._voice_client.is_listening():
            self._voice_client.stop_listening()
        if self._voice_client.is_playing():
            self._voice_client.stop()
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
        return (
            self._voice_bridging
            and _discord_voice_client.has_nacl
            and discord.opus.is_loaded()
            and _voice_recv_importable()
        )

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
        voice_recv = _voice_recv_module()
        try:
            voice_client = await channel.connect(cls=voice_recv.VoiceRecvClient)
        except Exception as exc:
            raise VoiceJoinError(
                f"failed to join Discord voice channel {channel_id!r} on connector {self.connector_id!r}: {exc}"
            ) from exc
        return DiscordVoiceTransport(self.connector_id, voice_client)
