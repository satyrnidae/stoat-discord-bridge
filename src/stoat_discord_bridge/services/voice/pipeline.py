"""Connector-neutral N-way audio mixing (issue #113 Phase 3).

**The mix is per-connector, not one shared bus.** Each joined connector gets
its own output stream containing only the *other* connectors' audio - a
connector's own speakers are never summed into its own output. That
invariant is structural here, not just upstream echo suppression:
`MixSource.mix` only ever reads entries whose `connector_id` differs from
its own, so it is not possible for a connector to hear itself through this
module regardless of what a transport pushes into the registry.

Everything in this module is synchronous, framework-free PCM math - no
asyncio, no discord.py/stoat.py/livekit imports - except `MixerClock`, whose
only asyncio dependency is the 20ms scheduling loop itself.

**Why a central clock, not per-connector pulls.** A speaker's queued frame
must reach *every other* joined connector's mix for that same tick, not just
whichever connector happens to read first - so popping a speaker's buffer
must happen exactly once per tick, shared across every listener. `MixerClock`
is that single pop: it ticks the shared `SpeakerRegistry` once every 20ms,
computes each joined connector's mix from that one snapshot, and stores the
result in that connector's `LatestFrameHolder`. A transport's own read path
(Discord's synchronous `AudioSource.read()`, Stoat's own 20ms
`capture_frame` loop) always reads the *holder*, never the registry or a
`MixSource` directly - so unevenly-timed per-connector clocks can't corrupt
each other's mix.

Frame shape is pinned to Discord's own PCM format (20ms / 48kHz / stereo /
16-bit signed, little-endian) since Discord's send/receive path uses it with
no conversion of its own; `resample_to_bridge` converts any other source
(a LiveKit track at a different rate/channel count) into this same shape
before it ever reaches a `JitterBuffer`.
"""

from __future__ import annotations

import asyncio
import audioop
import contextlib
from collections import deque

SAMPLE_RATE = 48000
CHANNELS = 2
SAMPLE_WIDTH = 2  # bytes per sample (16-bit signed PCM)
FRAME_MS = 20
SAMPLES_PER_FRAME = SAMPLE_RATE * FRAME_MS // 1000  # 960
FRAME_BYTES = SAMPLES_PER_FRAME * CHANNELS * SAMPLE_WIDTH  # 3840
SILENCE_FRAME = b"\x00" * FRAME_BYTES

_DEFAULT_JITTER_FRAMES = 10  # ~200ms of buffering per speaker


def pad_or_trim(frame: bytes) -> bytes:
    """Force `frame` to exactly `FRAME_BYTES` - a defensive normalization
    applied to every frame before it enters a `JitterBuffer`, since neither
    transport is guaranteed to hand back an exact 20ms chunk on every
    callback (a short final packet, a slightly-off LiveKit frame_size_ms)."""
    if len(frame) == FRAME_BYTES:
        return frame
    if len(frame) > FRAME_BYTES:
        return frame[:FRAME_BYTES]
    return frame + b"\x00" * (FRAME_BYTES - len(frame))


def resample_to_bridge(pcm: bytes, *, sample_rate: int, num_channels: int) -> bytes:
    """Convert a one-shot PCM frame (16-bit signed) at `sample_rate`/
    `num_channels` into the bridge's standard shape - 48kHz/stereo, padded
    or trimmed to exactly one `FRAME_BYTES` frame. Mono is upmixed to stereo
    (`audioop.tostereo`, both channels get the same sample); any channel
    count besides 1 or 2 isn't a real speaker feed and is rejected outright
    rather than silently mangled.

    No streaming resample state is kept across calls - each Discord/LiveKit
    callback hands over one independent 20ms frame, and `audioop.ratecv`'s
    edge artifacts from resetting state every call are inaudible at this
    frame size."""
    if num_channels == 1:
        pcm = audioop.tostereo(pcm, SAMPLE_WIDTH, 1, 1)
    elif num_channels != 2:
        raise ValueError(f"resample_to_bridge: unsupported channel count {num_channels!r} (expected 1 or 2)")
    if sample_rate != SAMPLE_RATE:
        pcm, _state = audioop.ratecv(pcm, SAMPLE_WIDTH, CHANNELS, sample_rate, SAMPLE_RATE, None)
    return pad_or_trim(pcm)


class JitterBuffer:
    """A small bounded FIFO of one speaker's queued 20ms frames. `push`
    silently drops the oldest queued frame on overflow (a `deque(maxlen=…)`
    discards from the left as new frames arrive) rather than growing
    unboundedly if a consumer stalls - a dropped frame here is a brief
    glitch, not a leak. `pop` returns `None` on underrun (nothing queued)
    rather than manufacturing silence - callers (`SpeakerRegistry.tick`)
    treat a `None` pop as "this speaker contributed nothing this tick", not
    as an explicit silent frame, so it costs a dict lookup skip instead of a
    wasted `audioop.add` against zero."""

    def __init__(self, maxlen: int = _DEFAULT_JITTER_FRAMES) -> None:
        self._frames: "deque[bytes]" = deque(maxlen=maxlen)

    def push(self, frame: bytes) -> None:
        self._frames.append(frame)

    def pop(self) -> "bytes | None":
        if not self._frames:
            return None
        return self._frames.popleft()

    def __len__(self) -> int:
        return len(self._frames)


class SpeakerRegistry:
    """Owns every live speaker's `JitterBuffer`, keyed `(connector_id,
    speaker_id)`, for one voice session. `tick()` is the single point where
    frames are actually consumed - see the module docstring for why that has
    to be centralized rather than pulled independently per connector."""

    def __init__(self, *, buffer_size: int = _DEFAULT_JITTER_FRAMES) -> None:
        self._buffer_size = buffer_size
        self._buffers: "dict[tuple[str, str], JitterBuffer]" = {}

    def push_frame(self, connector_id: str, speaker_id: str, frame: bytes) -> None:
        """Enqueue `frame` for `(connector_id, speaker_id)`, normalizing its
        length to `FRAME_BYTES` first (`pad_or_trim`) - the single point
        every frame passes through before reaching a `JitterBuffer`, so
        `MixSource.mix`'s `audioop.add` never sees mismatched frame lengths
        regardless of what a transport handed over."""
        key = (connector_id, speaker_id)
        buf = self._buffers.get(key)
        if buf is None:
            buf = JitterBuffer(maxlen=self._buffer_size)
            self._buffers[key] = buf
        buf.push(pad_or_trim(frame))

    def remove_speaker(self, connector_id: str, speaker_id: str) -> None:
        self._buffers.pop((connector_id, speaker_id), None)

    def remove_connector(self, connector_id: str) -> None:
        for key in [k for k in self._buffers if k[0] == connector_id]:
            del self._buffers[key]

    def tick(self) -> "dict[tuple[str, str], bytes]":
        """Pop exactly one frame from every speaker's buffer (an underrun
        speaker is simply absent from the result, not present with silence)
        - one snapshot per mixing tick, shared by every connector's
        `MixSource.mix` so a speaker's frame is never consumed by one
        listener at another's expense."""
        snapshot: "dict[tuple[str, str], bytes]" = {}
        for key, buf in self._buffers.items():
            frame = buf.pop()
            if frame is not None:
                snapshot[key] = frame
        return snapshot


class MixSource:
    """One joined connector's own output: `mix(snapshot)` sums the current
    tick's frame from every speaker *except* this connector's own -
    structurally impossible to include `exclude_connector_id`'s contribution
    since the loop skips it outright, never reads it. Clamps via
    `audioop.add`'s own int16 saturation rather than wrapping on overflow."""

    def __init__(self, exclude_connector_id: str) -> None:
        self.exclude_connector_id = exclude_connector_id

    def mix(self, snapshot: "dict[tuple[str, str], bytes]") -> bytes:
        result: "bytes | None" = None
        for (connector_id, _speaker_id), frame in snapshot.items():
            if connector_id == self.exclude_connector_id:
                continue
            result = frame if result is None else audioop.add(result, frame, SAMPLE_WIDTH)
        return SILENCE_FRAME if result is None else result


class LatestFrameHolder:
    """A single-slot "what should this connector play right now" cell,
    updated by `MixerClock` every tick and read by that connector's own
    transport. A plain attribute (not a queue) is deliberate: Discord's
    `AudioSource.read()` is called synchronously from discord.py's dedicated
    player thread and must return immediately, and a Stoat connector's own
    20ms `capture_frame` loop only ever wants the *latest* mix, never a
    backlog - a bare CPython attribute read/write is already atomic under
    the GIL, so no lock is needed for this single-writer/multi-reader use."""

    def __init__(self) -> None:
        self._frame: bytes = SILENCE_FRAME

    def read(self) -> bytes:
        return self._frame

    def set(self, frame: bytes) -> None:
        self._frame = frame


class MixerClock:
    """Owns one voice session's `SpeakerRegistry` and ticks it once every
    `FRAME_MS` milliseconds, updating every currently-joined connector's
    `LatestFrameHolder` from that tick's snapshot. `add_connector`/
    `remove_connector` are what `join_connector`/`part_connector` (Phase 2's
    coordinator) call as connectors come and go mid-session - dynamic,
    no restart needed."""

    def __init__(self, *, interval: float = FRAME_MS / 1000) -> None:
        self.registry = SpeakerRegistry()
        self._interval = interval
        self._connectors: "dict[str, tuple[MixSource, LatestFrameHolder]]" = {}
        self._task: "asyncio.Task | None" = None

    def add_connector(self, connector_id: str) -> LatestFrameHolder:
        """Idempotent: a repeat call for an already-registered `connector_id`
        returns its existing holder rather than replacing it - a transport
        that already holds a reference to that holder (via `set_output`)
        would otherwise silently stop receiving updates the moment a second
        `add_connector` call swapped in a fresh one underneath it."""
        existing = self._connectors.get(connector_id)
        if existing is not None:
            return existing[1]
        holder = LatestFrameHolder()
        self._connectors[connector_id] = (MixSource(connector_id), holder)
        return holder

    def remove_connector(self, connector_id: str) -> None:
        self._connectors.pop(connector_id, None)
        self.registry.remove_connector(connector_id)

    def tick_once(self) -> None:
        snapshot = self.registry.tick()
        for mix_source, holder in self._connectors.values():
            holder.set(mix_source.mix(snapshot))

    def start(self) -> None:
        """Start the periodic tick loop. Safe to call again - any previously
        running loop is cancelled first rather than leaked alongside a new
        one, matching `VoiceBridgeCoordinator.start`'s own restart pattern."""
        if self._task is not None:
            self._task.cancel()
        self._task = asyncio.create_task(self._run())

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(self._interval)
            self.tick_once()

    async def close(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task
        self._task = None
