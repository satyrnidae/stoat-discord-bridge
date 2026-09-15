"""Stoat's `VoiceConnector`/`VoiceTransport` (issue #113) - joins a Stoat
voice channel and holds the resulting `livekit.rtc.Room`. Phase 2 built the
join/leave lifecycle on top of that `Room`; Phase 3 adds real send/receive by
subscribing/publishing LiveKit tracks on the same connection - no new
connect call.

stoat.py's own `VoiceChannel.connect()` (`stoat.abc.Connectable.connect`)
already lazily imports `livekit.rtc` and raises `TypeError` if it isn't
installed, so this module doesn't need its own hard dependency on `livekit`
either - `voice_available` just does a cheap `importlib` probe (no import)
so the coordinator can skip a connector it already knows can't join, without
this module needing a new project dependency to check that. The real
`livekit.rtc` import for send/receive is deferred the same way, inside
`start`/`set_output`, once a `Room` already exists (so `livekit` is
confirmed installed by then)."""

from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import logging
from typing import TYPE_CHECKING

from stoat_discord_bridge.services.voice import pipeline
from stoat_discord_bridge.services.voice.base import VoiceJoinError, VoiceTransport

if TYPE_CHECKING:
    import livekit.rtc as rtc
    import stoat

logger = logging.getLogger(__name__)


def _livekit_importable() -> bool:
    return importlib.util.find_spec("livekit.rtc") is not None


def _open_audio_stream(track: "rtc.Track") -> "rtc.AudioStream":
    """Wraps `rtc.AudioStream(...)` construction as its own seam so tests
    can monkeypatch it with a fake async-iterable stream - the real one is
    FFI-backed and can't be constructed from a bare stub track."""
    import livekit.rtc as rtc

    return rtc.AudioStream(
        track,
        sample_rate=pipeline.SAMPLE_RATE,
        num_channels=pipeline.CHANNELS,
        frame_size_ms=pipeline.FRAME_MS,
    )


def _create_publish_track() -> "tuple[rtc.AudioSource, rtc.LocalAudioTrack]":
    """Wraps `rtc.AudioSource`/`rtc.LocalAudioTrack.create_audio_track`
    construction as its own seam, same rationale as `_open_audio_stream` -
    both are FFI-backed and can't be faked directly."""
    import livekit.rtc as rtc

    audio_source = rtc.AudioSource(pipeline.SAMPLE_RATE, pipeline.CHANNELS)
    local_track = rtc.LocalAudioTrack.create_audio_track("bridge", audio_source)
    return audio_source, local_track


def _build_audio_frame(pcm: bytes) -> "rtc.AudioFrame":
    import livekit.rtc as rtc

    return rtc.AudioFrame(pcm, pipeline.SAMPLE_RATE, pipeline.CHANNELS, pipeline.SAMPLES_PER_FRAME)


class StoatVoiceTransport(VoiceTransport):
    def __init__(self, connector_id: str, room: "rtc.Room") -> None:
        self.connector_id = connector_id
        self._room = room
        self._on_speaker_frame = None
        self._consume_tasks: "dict[str, asyncio.Task]" = {}
        # Consume tasks cancelled by a track_subscribed refire (see
        # _on_track_subscribed) but not yet awaited - close() must still
        # wait on these, since they're no longer reachable via
        # _consume_tasks once that dict entry is overwritten by the new task.
        self._retiring_tasks: "list[asyncio.Task]" = []
        self._publish_task: "asyncio.Task | None" = None

    async def start(self, on_speaker_frame) -> None:
        """Subscribe to every remote participant's audio track. LiveKit
        re-fires `track_subscribed` for tracks that were already publishing
        before we joined (not just new ones), so this alone is enough to
        pick up occupants who were already in the call.

        The `on(...)` callback runs on the same asyncio loop `Room` itself
        uses to process LiveKit events (unlike Discord's receive thread), so
        it can schedule a consume task directly - no thread hop needed."""
        self._on_speaker_frame = on_speaker_frame
        self._room.on("track_subscribed", self._on_track_subscribed)

    def _on_track_subscribed(self, track: "rtc.Track", _publication: object, participant: "rtc.RemoteParticipant") -> None:
        import livekit.rtc as rtc

        if track.kind != rtc.TrackKind.KIND_AUDIO:
            return
        # Second-line echo guard (belt and braces on top of the mix-minus
        # filter itself): never buffer this connector's own published track
        # as if it were a remote speaker. LiveKit only fires
        # track_subscribed for *remote* tracks in practice, but this is
        # cheap insurance against ever crossing that line.
        local = self._room.local_participant
        if local is not None and participant.identity == local.identity:
            return
        # track_subscribed can refire for an identity already being
        # consumed (a republish/reconnect) - cancel the stale task first
        # rather than just overwriting the dict entry, which would orphan
        # it: still running, its AudioStream never closed, and no longer
        # reachable from close() once this line replaces it. Cancelling
        # alone isn't enough either - close()'s cleanup loops only iterate
        # _consume_tasks, so a cancelled-but-unawaited task would still let
        # close() return before that task's own `finally: await
        # stream.aclose()` has actually run; parking it on _retiring_tasks
        # is what makes close() wait for it too.
        existing = self._consume_tasks.get(participant.identity)
        if existing is not None:
            existing.cancel()
            self._retiring_tasks.append(existing)
        self._consume_tasks[participant.identity] = asyncio.create_task(self._consume_track(track, participant))

    async def _consume_track(self, track: "rtc.Track", participant: "rtc.RemoteParticipant") -> None:
        stream = _open_audio_stream(track)
        try:
            async for event in stream:
                frame = bytes(event.frame.data)
                await self._on_speaker_frame(self.connector_id, participant.identity, frame)
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception(
                "[voice] error consuming audio stream for connector %s participant %s",
                self.connector_id,
                participant.identity,
            )
        finally:
            await stream.aclose()

    def set_output(self, source: "pipeline.LatestFrameHolder") -> None:
        """Start publishing `source`'s frames as this connector's own
        LiveKit audio track. Runs as a background task (`set_output` itself
        is synchronous, per `VoiceTransport`) that publishes once, then
        pulls the latest mix every 20ms and pushes it via `capture_frame` -
        LiveKit has no pull-based playback API like Discord's
        `AudioSource.read()`, so the bridge has to drive this loop itself."""
        self._publish_task = asyncio.create_task(self._run_publish(source))

    async def _run_publish(self, source: "pipeline.LatestFrameHolder") -> None:
        import livekit.rtc as rtc

        audio_source, local_track = _create_publish_track()
        try:
            # source=SOURCE_MICROPHONE (default is SOURCE_UNKNOWN) - Stoat's
            # server tracks each voice-channel member's `is_publishing` state
            # off a webhook-driven check for a published *microphone* track,
            # not just any published track; publishing without this the bot
            # joined and its track was live at the LiveKit layer, but Stoat
            # never flipped it out of "muted" and no client ever rendered
            # its audio.
            await self._room.local_participant.publish_track(
                local_track, rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE)
            )
            while True:
                await audio_source.capture_frame(_build_audio_frame(source.read()))
                await asyncio.sleep(pipeline.FRAME_MS / 1000)
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("[voice] error publishing audio for connector %s", self.connector_id)

    async def close(self) -> None:
        if self._publish_task is not None:
            self._publish_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._publish_task
            self._publish_task = None
        all_tasks = [*self._consume_tasks.values(), *self._retiring_tasks]
        for task in all_tasks:
            task.cancel()
        for task in all_tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._consume_tasks.clear()
        self._retiring_tasks.clear()
        await self._room.disconnect()


class StoatVoiceConnector:
    """Owns one Stoat connector's voice-join capability -
    `VoiceBridgeCoordinator` holds one of these per Stoat connector. Channel
    resolution tries the cache first, falling back to a live `fetch_channel`
    on a miss, matching `channel_is_voice`/`voice_occupants`'s own lookup
    pattern (`lookups/names.py`'s `_resolve_voice_channel`) - so a channel
    `refresh_groups` only just classified as voice-bridgeable via that same
    fallback doesn't then fail to `join()` for the identical reason."""

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
            try:
                channel = await self._client.fetch_channel(channel_id)
            except Exception as exc:
                raise VoiceJoinError(
                    f"Stoat voice channel {channel_id!r} not found on connector {self.connector_id!r}: {exc}"
                ) from exc
        try:
            room = await channel.connect(node=self._voice_node)
        except Exception as exc:
            raise VoiceJoinError(
                f"failed to join Stoat voice channel {channel_id!r} on connector {self.connector_id!r}: {exc}"
            ) from exc
        return StoatVoiceTransport(self.connector_id, room)
