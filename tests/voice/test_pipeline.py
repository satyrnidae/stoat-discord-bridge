"""Pure-logic tests for `services/voice/pipeline.py` (issue #113 Phase 3) -
framing/resample helpers, the per-speaker `JitterBuffer`, `SpeakerRegistry`'s
per-tick snapshot, and `MixSource`'s N-way mix-minus invariant. No asyncio,
no discord.py/stoat.py/livekit - these are pure byte-crunching functions, the
bulk of Phase 3's coverage per the issue's own testing strategy.
"""

from __future__ import annotations

import struct

from stoat_discord_bridge.services.voice import pipeline


def _tone(*samples: int, channels: int = 2) -> bytes:
    """Build one raw PCM frame from explicit per-channel-sample int16
    values, e.g. `_tone(100, 100, -50, -50)` for two stereo sample-frames."""
    return struct.pack(f"<{len(samples)}h", *samples)


def _frame(value: int, *, samples: int = pipeline.SAMPLES_PER_FRAME) -> bytes:
    """A full FRAME_BYTES-length stereo frame where every sample (both
    channels) is `value`."""
    return struct.pack(f"<{samples * pipeline.CHANNELS}h", *([value] * samples * pipeline.CHANNELS))


# ---------------------------------------------------------------- constants


def test_frame_shape_matches_discord_20ms_48khz_stereo_s16():
    # discord.py's own opus.Encoder: SAMPLES_PER_FRAME=960, FRAME_SIZE=3840 -
    # the bridge's frame shape must match exactly, since Discord's transport
    # sends/receives PCM in this shape with no conversion of its own.
    assert pipeline.SAMPLE_RATE == 48000
    assert pipeline.CHANNELS == 2
    assert pipeline.SAMPLE_WIDTH == 2
    assert pipeline.SAMPLES_PER_FRAME == 960
    assert pipeline.FRAME_BYTES == 3840
    assert len(pipeline.SILENCE_FRAME) == pipeline.FRAME_BYTES
    assert pipeline.SILENCE_FRAME == b"\x00" * 3840


# ---------------------------------------------------------------- pad_or_trim


def test_pad_or_trim_pads_short_frame_with_silence():
    short = _tone(100, 100)
    padded = pipeline.pad_or_trim(short)
    assert len(padded) == pipeline.FRAME_BYTES
    assert padded[:4] == short
    assert padded[4:] == b"\x00" * (pipeline.FRAME_BYTES - 4)


def test_pad_or_trim_trims_long_frame():
    long_frame = b"\x01\x02" * (pipeline.SAMPLES_PER_FRAME * pipeline.CHANNELS + 10)
    trimmed = pipeline.pad_or_trim(long_frame)
    assert len(trimmed) == pipeline.FRAME_BYTES
    assert trimmed == long_frame[: pipeline.FRAME_BYTES]


def test_pad_or_trim_exact_length_is_unchanged():
    exact = pipeline.SILENCE_FRAME
    assert pipeline.pad_or_trim(exact) == exact


# ---------------------------------------------------------------- resample_to_bridge


def test_resample_to_bridge_upmixes_mono_to_stereo():
    mono = struct.pack("<3h", 1000, 2000, 3000)
    out = pipeline.resample_to_bridge(mono, sample_rate=48000, num_channels=1)
    assert len(out) == pipeline.FRAME_BYTES
    # Each mono sample duplicated onto both channels (audioop.tostereo, factor 1/1).
    assert out[:12] == struct.pack("<6h", 1000, 1000, 2000, 2000, 3000, 3000)


def test_resample_to_bridge_resamples_rate():
    # 24kHz mono input, one 20ms frame = 480 samples -> upsampled to 48kHz
    # stereo should land on the standard FRAME_BYTES length.
    samples = [1000] * 480
    pcm = struct.pack(f"<{len(samples)}h", *samples)
    out = pipeline.resample_to_bridge(pcm, sample_rate=24000, num_channels=1)
    assert len(out) == pipeline.FRAME_BYTES


def test_resample_to_bridge_stereo_48k_is_pad_or_trim_only():
    frame = _frame(500)
    out = pipeline.resample_to_bridge(frame, sample_rate=48000, num_channels=2)
    assert out == frame


def test_resample_to_bridge_rejects_unsupported_channel_count():
    import pytest

    with pytest.raises(ValueError):
        pipeline.resample_to_bridge(b"\x00" * 100, sample_rate=48000, num_channels=3)


# ---------------------------------------------------------------- JitterBuffer


def test_jitter_buffer_pop_returns_pushed_frames_in_order():
    buf = pipeline.JitterBuffer()
    buf.push(_frame(1))
    buf.push(_frame(2))
    assert buf.pop() == _frame(1)
    assert buf.pop() == _frame(2)


def test_jitter_buffer_pop_returns_none_on_underrun():
    buf = pipeline.JitterBuffer()
    assert buf.pop() is None


def test_jitter_buffer_drops_oldest_frame_on_overflow():
    buf = pipeline.JitterBuffer(maxlen=2)
    buf.push(_frame(1))
    buf.push(_frame(2))
    buf.push(_frame(3))
    assert buf.pop() == _frame(2)
    assert buf.pop() == _frame(3)
    assert buf.pop() is None


def test_jitter_buffer_len_reflects_queued_frames():
    buf = pipeline.JitterBuffer()
    assert len(buf) == 0
    buf.push(_frame(1))
    assert len(buf) == 1
    buf.pop()
    assert len(buf) == 0


# ---------------------------------------------------------------- SpeakerRegistry.tick


def test_registry_tick_pops_one_frame_per_speaker():
    registry = pipeline.SpeakerRegistry()
    registry.push_frame("discord", "u1", _frame(10))
    registry.push_frame("discord", "u1", _frame(20))
    registry.push_frame("stoat", "u2", _frame(30))

    snapshot = registry.tick()

    assert snapshot == {("discord", "u1"): _frame(10), ("stoat", "u2"): _frame(30)}
    # Second tick drains the next queued frame for the speaker that had two.
    snapshot2 = registry.tick()
    assert snapshot2 == {("discord", "u1"): _frame(20)}


def test_registry_tick_skips_underrun_speakers_entirely():
    registry = pipeline.SpeakerRegistry()
    registry.push_frame("discord", "u1", _frame(10))
    registry.remove_speaker("discord", "u1")
    assert registry.tick() == {}


def test_registry_remove_connector_drops_all_its_speakers():
    registry = pipeline.SpeakerRegistry()
    registry.push_frame("discord", "u1", _frame(1))
    registry.push_frame("discord", "u2", _frame(2))
    registry.push_frame("stoat", "u3", _frame(3))

    registry.remove_connector("discord")

    assert registry.tick() == {("stoat", "u3"): _frame(3)}


def test_registry_push_frame_creates_buffer_lazily_and_reuses_it():
    registry = pipeline.SpeakerRegistry()
    registry.push_frame("discord", "u1", _frame(1))
    registry.push_frame("discord", "u1", _frame(2))
    assert registry.tick() == {("discord", "u1"): _frame(1)}
    assert registry.tick() == {("discord", "u1"): _frame(2)}


def test_registry_push_frame_normalizes_length_before_buffering():
    # A short/odd-length frame (Discord's own "short final packet" case)
    # must not reach MixSource.mix()'s audioop.add un-normalized - that
    # would raise on a length mismatch against any full-length frame.
    registry = pipeline.SpeakerRegistry()
    registry.push_frame("discord", "u1", struct.pack("<2h", 111, 222))
    snapshot = registry.tick()
    assert snapshot[("discord", "u1")] == pipeline.pad_or_trim(struct.pack("<2h", 111, 222))
    assert len(snapshot[("discord", "u1")]) == pipeline.FRAME_BYTES


# ---------------------------------------------------------------- MixSource.mix


def test_mix_source_sums_two_non_excluded_speakers():
    snapshot = {
        ("stoat", "a"): _tone(100, 100),
        ("stoat_sh", "b"): _tone(200, 200),
        ("discord", "c"): _tone(9999, 9999),
    }
    mix = pipeline.MixSource("discord")
    out = mix.mix(snapshot)
    assert out == _tone(300, 300)


def test_mix_source_never_contains_its_own_connectors_contribution():
    # Even as the ONLY speaker present, a connector's own audio must never
    # appear in its own mix - the structural "no loopback" invariant.
    snapshot = {("discord", "self"): _tone(12345, 12345)}
    mix = pipeline.MixSource("discord")
    assert mix.mix(snapshot) == pipeline.SILENCE_FRAME


def test_mix_source_three_way_excludes_only_its_own_connector():
    snapshot = {
        ("discord", "a"): _tone(100, 0),
        ("stoat", "b"): _tone(0, 200),
        ("stoat_sh", "c"): _tone(50, 50),
    }
    assert pipeline.MixSource("discord").mix(snapshot) == _tone(50, 250)
    assert pipeline.MixSource("stoat").mix(snapshot) == _tone(150, 50)
    assert pipeline.MixSource("stoat_sh").mix(snapshot) == _tone(100, 200)


def test_mix_source_returns_silence_on_empty_snapshot():
    assert pipeline.MixSource("discord").mix({}) == pipeline.SILENCE_FRAME


def test_mix_source_clamps_on_overflow_rather_than_wrapping():
    snapshot = {
        ("stoat", "a"): _tone(30000, -30000),
        ("stoat_sh", "b"): _tone(30000, -30000),
    }
    out = pipeline.MixSource("discord").mix(snapshot)
    assert out == _tone(32767, -32768)


# ---------------------------------------------------------------- LatestFrameHolder


def test_latest_frame_holder_defaults_to_silence():
    holder = pipeline.LatestFrameHolder()
    assert holder.read() == pipeline.SILENCE_FRAME


def test_latest_frame_holder_returns_last_set_frame():
    holder = pipeline.LatestFrameHolder()
    holder.set(_frame(7))
    assert holder.read() == _frame(7)
    holder.set(_frame(8))
    assert holder.read() == _frame(8)


# ---------------------------------------------------------------- MixerClock


def test_mixer_clock_add_connector_returns_a_holder_fed_by_tick_once():
    clock = pipeline.MixerClock()
    holder = clock.add_connector("discord")
    clock.registry.push_frame("stoat", "u1", _frame(100))

    clock.tick_once()

    assert holder.read() == _frame(100)


def test_mixer_clock_tick_once_excludes_each_connectors_own_speakers():
    clock = pipeline.MixerClock()
    discord_holder = clock.add_connector("discord")
    stoat_holder = clock.add_connector("stoat")
    clock.registry.push_frame("discord", "d1", _frame(10))
    clock.registry.push_frame("stoat", "s1", _frame(20))

    clock.tick_once()

    assert discord_holder.read() == _frame(20)
    assert stoat_holder.read() == _frame(10)


def test_mixer_clock_remove_connector_stops_updating_its_holder_and_excludes_it_from_others():
    clock = pipeline.MixerClock()
    discord_holder = clock.add_connector("discord")
    stoat_holder = clock.add_connector("stoat")
    clock.registry.push_frame("discord", "d1", _tone(10, 10))

    clock.remove_connector("discord")
    clock.registry.push_frame("stoat", "s1", _tone(20, 20))
    clock.tick_once()

    # discord_holder is frozen at whatever it last had (silence - never ticked).
    assert discord_holder.read() == pipeline.SILENCE_FRAME
    assert stoat_holder.read() == pipeline.SILENCE_FRAME  # only its own speaker was present


def test_mixer_clock_add_connector_twice_returns_the_same_holder():
    # A second add_connector for an already-registered id must not swap in a
    # fresh holder - a transport already reading the first one would
    # otherwise silently stop seeing updates.
    clock = pipeline.MixerClock()
    first = clock.add_connector("discord")
    second = clock.add_connector("discord")
    assert first is second

    clock.registry.push_frame("stoat", "u1", _frame(5))
    clock.tick_once()
    assert first.read() == _frame(5)


async def test_mixer_clock_run_stops_cleanly_on_close():
    clock = pipeline.MixerClock(interval=0.001)
    clock.add_connector("discord")
    clock.start()
    await clock.close()  # must not raise or hang
