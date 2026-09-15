"""`StoatVoiceConnector`/`StoatVoiceTransport` (issue #113) - real join/close
(Phase 2) and real send/receive wiring (Phase 3) against
`FakeClient`/`FakeVoiceChannel`/`FakeStoatRoom`, no real stoat.py
network/livekit involved. The genuinely FFI-backed livekit objects
(`AudioStream`, `AudioSource`, `LocalAudioTrack`) are never constructed here
- `_open_audio_stream`/`_create_publish_track` are monkeypatched seams
instead, same rationale as their docstrings in `stoat_service/voice.py`.
`livekit.rtc.TrackKind` itself IS the real enum (`livekit` is a genuine
installed dependency, the `voice` extra) since referencing it needs no FFI
object construction.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from livekit import rtc

from stoat_discord_bridge.services.stoat_service.voice import StoatVoiceConnector
from stoat_discord_bridge.services.voice import pipeline
from stoat_discord_bridge.services.voice.base import VoiceJoinError
from tests.fakes.fake_stoat import FakeChannel, FakeClient, FakeLocalParticipant, FakeStoatRoom, FakeVoiceChannel


def test_voice_available_true_when_bridging_on_and_livekit_importable(monkeypatch):
    import stoat_discord_bridge.services.stoat_service.voice as voice_mod

    monkeypatch.setattr(voice_mod, "_livekit_importable", lambda: True)
    connector = StoatVoiceConnector("stoat", FakeClient(), voice_bridging=True)
    assert connector.voice_available is True


def test_voice_available_false_when_voice_bridging_off(monkeypatch):
    import stoat_discord_bridge.services.stoat_service.voice as voice_mod

    monkeypatch.setattr(voice_mod, "_livekit_importable", lambda: True)
    connector = StoatVoiceConnector("stoat", FakeClient(), voice_bridging=False)
    assert connector.voice_available is False


def test_voice_available_false_when_livekit_not_importable(monkeypatch):
    import stoat_discord_bridge.services.stoat_service.voice as voice_mod

    monkeypatch.setattr(voice_mod, "_livekit_importable", lambda: False)
    connector = StoatVoiceConnector("stoat", FakeClient(), voice_bridging=True)
    assert connector.voice_available is False


async def test_join_connects_and_returns_transport():
    client = FakeClient()
    room = FakeStoatRoom()
    channel = client.add_channel(FakeVoiceChannel("c1", connect_result=room))
    connector = StoatVoiceConnector("stoat", client, voice_bridging=True, voice_node="worldwide")

    transport = await connector.join("c1")

    assert transport.connector_id == "stoat"
    assert channel.connect_calls == ["worldwide"]


async def test_join_unknown_channel_raises_voice_join_error():
    connector = StoatVoiceConnector("stoat", FakeClient(), voice_bridging=True)
    with pytest.raises(VoiceJoinError):
        await connector.join("missing")


async def test_join_falls_back_to_a_live_fetch_on_a_cache_miss():
    """A channel `refresh_groups` only classified as voice-bridgeable via
    `_resolve_voice_channel`'s own fetch fallback (issue #66's cache drift)
    must resolve the same way here, or the session that classification just
    allowed would immediately fail to join."""
    client = FakeClient()
    room = FakeStoatRoom()
    channel = client.set_fetched_channel(FakeVoiceChannel("c1", connect_result=room))
    connector = StoatVoiceConnector("stoat", client, voice_bridging=True, voice_node="worldwide")

    transport = await connector.join("c1")

    assert transport.connector_id == "stoat"
    assert channel.connect_calls == ["worldwide"]


async def test_join_connect_failure_raises_voice_join_error():
    client = FakeClient()
    client.add_channel(FakeVoiceChannel("c1", connect_error=TypeError("Livekit is unavailable")))
    connector = StoatVoiceConnector("stoat", client, voice_bridging=True)
    with pytest.raises(VoiceJoinError):
        await connector.join("c1")


async def test_join_non_voice_channel_that_raises_on_connect_wraps_as_voice_join_error():
    """Unlike Discord, a Stoat TextChannel technically has a `connect()`
    method too (stoat.abc.Connectable) - the server itself rejects joining
    one (CannotJoinCall), so this connector doesn't isinstance-check the
    channel, it just relies on the (wrapped) HTTPException from a bad
    connect() call, exercised here via a plain non-voice FakeChannel with no
    connect() at all raising AttributeError."""
    client = FakeClient()
    client.add_channel(FakeChannel("c1", name="text"))
    connector = StoatVoiceConnector("stoat", client, voice_bridging=True)
    with pytest.raises(VoiceJoinError):
        await connector.join("c1")


async def test_transport_close_disconnects_room():
    room = FakeStoatRoom()
    client = FakeClient()
    client.add_channel(FakeVoiceChannel("c1", connect_result=room))
    connector = StoatVoiceConnector("stoat", client, voice_bridging=True)
    transport = await connector.join("c1")

    await transport.close()

    assert room.disconnect_calls == 1


# ---------------------------------------------------------------- Phase 3: receive


class _FakeAudioStream:
    """Stands in for livekit.rtc.AudioStream - an async-iterable of
    SimpleNamespace(frame=SimpleNamespace(data=<bytes>)), matching the real
    AudioFrameEvent shape closely enough for `_consume_track` to unwrap."""

    def __init__(self, frames: "list[bytes]") -> None:
        self._frames = list(frames)
        self.closed = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._frames:
            # Real AudioStream never raises StopAsyncIteration on its own -
            # a still-subscribed track just has no next frame yet. Block
            # forever here too, so tests control the end of iteration via
            # cancellation (transport.close()), matching production.
            await asyncio.Future()
        return SimpleNamespace(frame=SimpleNamespace(data=self._frames.pop(0)))

    async def aclose(self) -> None:
        self.closed = True


async def _joined_transport(room: "FakeStoatRoom | None" = None):
    room = room or FakeStoatRoom()
    client = FakeClient()
    client.add_channel(FakeVoiceChannel("c1", connect_result=room))
    connector = StoatVoiceConnector("stoat", client, voice_bridging=True)
    transport = await connector.join("c1")
    return transport, room


async def test_start_subscribes_to_track_subscribed():
    transport, room = await _joined_transport()

    await transport.start(lambda *a, **k: None)

    assert room._listeners["track_subscribed"] == transport._on_track_subscribed


def test_on_track_subscribed_skips_non_audio_tracks():
    room = FakeStoatRoom()
    from stoat_discord_bridge.services.stoat_service.voice import StoatVoiceTransport

    transport = StoatVoiceTransport("stoat", room)
    track = SimpleNamespace(kind=rtc.TrackKind.KIND_VIDEO)
    participant = SimpleNamespace(identity="remote-user")

    transport._on_track_subscribed(track, None, participant)

    assert transport._consume_tasks == {}


def test_on_track_subscribed_skips_local_participants_own_track():
    room = FakeStoatRoom()
    from stoat_discord_bridge.services.stoat_service.voice import StoatVoiceTransport

    transport = StoatVoiceTransport("stoat", room)
    track = SimpleNamespace(kind=rtc.TrackKind.KIND_AUDIO)
    participant = SimpleNamespace(identity=room.local_participant.identity)

    transport._on_track_subscribed(track, None, participant)

    assert transport._consume_tasks == {}


async def test_on_track_subscribed_routes_remote_audio_frames(monkeypatch):
    import stoat_discord_bridge.services.stoat_service.voice as voice_mod

    frame_bytes = b"\x01" * pipeline.FRAME_BYTES
    fake_stream = _FakeAudioStream([frame_bytes])
    monkeypatch.setattr(voice_mod, "_open_audio_stream", lambda track: fake_stream)

    transport, room = await _joined_transport()
    calls: list = []

    async def on_speaker_frame(connector_id, user_id, frame):
        calls.append((connector_id, user_id, frame))

    await transport.start(on_speaker_frame)
    track = SimpleNamespace(kind=rtc.TrackKind.KIND_AUDIO)
    participant = SimpleNamespace(identity="remote-user")

    room.trigger("track_subscribed", track, None, participant)
    await asyncio.sleep(0)  # let the scheduled consume task run one frame

    assert calls == [("stoat", "remote-user", frame_bytes)]

    await transport.close()  # cancel the still-iterating consume task
    assert fake_stream.closed is True


async def test_on_track_subscribed_refiring_for_same_identity_cancels_the_stale_task(monkeypatch):
    """A republish/reconnect re-fires track_subscribed for an identity
    already being consumed - the stale task must be cancelled, not just
    silently replaced in the dict (which would orphan it: still running,
    its AudioStream never closed)."""
    import stoat_discord_bridge.services.stoat_service.voice as voice_mod

    first_stream = _FakeAudioStream([])
    second_stream = _FakeAudioStream([])
    streams = [first_stream, second_stream]
    monkeypatch.setattr(voice_mod, "_open_audio_stream", lambda track: streams.pop(0))

    transport, room = await _joined_transport()
    await transport.start(lambda *a, **k: None)
    track = SimpleNamespace(kind=rtc.TrackKind.KIND_AUDIO)
    participant = SimpleNamespace(identity="remote-user")

    room.trigger("track_subscribed", track, None, participant)
    await asyncio.sleep(0)
    first_task = transport._consume_tasks["remote-user"]

    room.trigger("track_subscribed", track, None, participant)
    await asyncio.sleep(0)
    second_task = transport._consume_tasks["remote-user"]

    assert first_task is not second_task
    assert first_task.done()
    assert first_stream.closed is True

    await transport.close()


# ---------------------------------------------------------------- Phase 3: send


class _FakeAudioSource:
    def __init__(self) -> None:
        self.captured: "list[bytes]" = []

    async def capture_frame(self, frame: bytes) -> None:
        self.captured.append(frame)


async def test_set_output_publishes_and_loops_capture_frame(monkeypatch):
    import stoat_discord_bridge.services.stoat_service.voice as voice_mod

    fake_source = _FakeAudioSource()
    monkeypatch.setattr(voice_mod, "_create_publish_track", lambda: (fake_source, "fake-local-track"))

    transport, room = await _joined_transport()
    holder = pipeline.LatestFrameHolder()
    holder.set(b"\x07" * pipeline.FRAME_BYTES)

    transport.set_output(holder)
    await asyncio.sleep(0.05)  # let a couple of 20ms capture_frame ticks run
    await transport.close()

    assert room.local_participant.publish_track_calls == ["fake-local-track"]
    # source=SOURCE_MICROPHONE - Stoat's server gates a member's
    # `is_publishing` (unmuted) state on a published *microphone* track,
    # not just any published track; the default (SOURCE_UNKNOWN) left the
    # bot showing as muted with no client ever rendering its audio, even
    # though the track was live at the LiveKit layer.
    (published_options,) = room.local_participant.publish_options_calls
    assert published_options.source == rtc.TrackSource.SOURCE_MICROPHONE
    assert fake_source.captured
    # capture_frame receives a real rtc.AudioFrame (only track/source
    # construction is faked here) - unwrap .data to check the PCM itself.
    assert all(bytes(frame.data) == b"\x07" * pipeline.FRAME_BYTES for frame in fake_source.captured)


async def test_close_cancels_consume_and_publish_tasks(monkeypatch):
    import stoat_discord_bridge.services.stoat_service.voice as voice_mod

    fake_stream = _FakeAudioStream([])
    monkeypatch.setattr(voice_mod, "_open_audio_stream", lambda track: fake_stream)
    fake_source = _FakeAudioSource()
    monkeypatch.setattr(voice_mod, "_create_publish_track", lambda: (fake_source, "fake-local-track"))

    transport, room = await _joined_transport()
    await transport.start(lambda *a, **k: None)
    transport.set_output(pipeline.LatestFrameHolder())
    room.trigger("track_subscribed", SimpleNamespace(kind=rtc.TrackKind.KIND_AUDIO), None, SimpleNamespace(identity="u1"))
    await asyncio.sleep(0)
    consume_task = transport._consume_tasks["u1"]
    publish_task = transport._publish_task

    await transport.close()

    assert consume_task.done()
    assert publish_task.done()
    assert transport._consume_tasks == {}
    assert room.disconnect_calls == 1


async def test_close_awaits_retired_tasks_from_a_track_subscribed_refire(monkeypatch):
    """close() must not return while a task retired by a track_subscribed
    refire (test_on_track_subscribed_refiring_for_same_identity_cancels_the_
    stale_task, above) is still mid-cancellation - both streams should be
    fully closed by the time close() returns, and _retiring_tasks emptied."""
    import stoat_discord_bridge.services.stoat_service.voice as voice_mod

    first_stream = _FakeAudioStream([])
    second_stream = _FakeAudioStream([])
    streams = [first_stream, second_stream]
    monkeypatch.setattr(voice_mod, "_open_audio_stream", lambda track: streams.pop(0))

    transport, room = await _joined_transport()
    await transport.start(lambda *a, **k: None)
    track = SimpleNamespace(kind=rtc.TrackKind.KIND_AUDIO)
    participant = SimpleNamespace(identity="remote-user")

    room.trigger("track_subscribed", track, None, participant)
    await asyncio.sleep(0)
    room.trigger("track_subscribed", track, None, participant)
    await asyncio.sleep(0)

    await transport.close()

    assert first_stream.closed is True
    assert second_stream.closed is True
    assert transport._retiring_tasks == []
