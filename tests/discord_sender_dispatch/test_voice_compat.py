"""`apply_voice_recv_patches` (issue #113 Phase 3) - discord-ext-voice-recv
0.5.2a179 predates Discord's DAVE (end-to-end encryption) rollout and has no
per-packet error handling, so `opus.PacketDecoder._decode_packet` needs two
fixes: decrypting a still-DAVE-encrypted payload before decode (mirroring
upstream PR #58), and substituting a silent frame for a packet that still
fails to decode afterward, rather than letting `discord.opus.OpusError`
propagate and kill the whole receive connection
(`PacketRouter.run()`'s `except Exception` calls `stop_listening()` in its
`finally`). Exercised against fake module/decoder/session shapes, not the
real discord-ext-voice-recv package or a live voice connection.
"""

from __future__ import annotations

from types import SimpleNamespace

import davey
import discord.opus
import pytest

from stoat_discord_bridge.services.discord_service._compat import apply_voice_recv_patches


def _fake_voice_recv_module(*, raise_on_decode: Exception | None = None):
    class _FakeDecoder:
        def __init__(self, ssrc: int) -> None:
            self.ssrc = ssrc

        def _decode_packet(self, packet):
            if raise_on_decode is not None:
                raise raise_on_decode
            return packet, b"real-pcm"

    return SimpleNamespace(opus=SimpleNamespace(PacketDecoder=_FakeDecoder))


class _FakeDaveSession:
    def __init__(
        self, *, ready: bool = True, decrypt_result: bytes = b"plaintext-opus", raise_on_decrypt: Exception | None = None
    ) -> None:
        self.ready = ready
        self._decrypt_result = decrypt_result
        self._raise_on_decrypt = raise_on_decrypt
        self.decrypt_calls: list = []

    def decrypt(self, user_id, media_type, packet: bytes) -> bytes:
        self.decrypt_calls.append((user_id, media_type, packet))
        if self._raise_on_decrypt is not None:
            raise self._raise_on_decrypt
        return self._decrypt_result


class _FakePacket:
    def __init__(self, decrypted_data: bytes) -> None:
        self.decrypted_data = decrypted_data

    def __bool__(self) -> bool:
        return True


def _fake_module_with_dave(*, dave_session: "_FakeDaveSession | None", cached_id: "int | None" = 123):
    """A `voice_recv`-shaped module whose `PacketDecoder._decode_packet`
    returns `(packet, b"decoded-" + packet.decrypted_data)` - lets a test
    tell whether DAVE decryption actually ran (and what it produced) by
    checking the decoded output, not just the session's own call log."""

    class _FakeConnection:
        def __init__(self) -> None:
            self.dave_session = dave_session
            self.dave_protocol_version = 1 if dave_session is not None else 0

    class _FakeVoiceClient:
        def __init__(self) -> None:
            self._connection = _FakeConnection()

    class _FakeSink:
        def __init__(self) -> None:
            self.voice_client = _FakeVoiceClient()

    class _FakeDecoder:
        def __init__(self, ssrc: int) -> None:
            self.ssrc = ssrc
            self.sink = _FakeSink()
            self._cached_id = cached_id

        def _decode_packet(self, packet):
            return packet, b"decoded-" + packet.decrypted_data

    return SimpleNamespace(opus=SimpleNamespace(PacketDecoder=_FakeDecoder))


def _opus_error() -> discord.opus.OpusError:
    # Bypasses __init__ (which shells out to libopus's opus_strerror via
    # ctypes) - these tests only need a real instance of the type the patch
    # catches, not a fully-constructed one with a native error string.
    return discord.opus.OpusError.__new__(discord.opus.OpusError)


def test_normal_decode_is_unaffected():
    module = _fake_voice_recv_module()
    apply_voice_recv_patches(module)
    decoder = module.opus.PacketDecoder(ssrc=7)

    assert decoder._decode_packet("packet") == ("packet", b"real-pcm")


def test_opus_error_is_replaced_with_a_silent_frame_of_the_right_size():
    module = _fake_voice_recv_module(raise_on_decode=_opus_error())
    apply_voice_recv_patches(module)
    decoder = module.opus.PacketDecoder(ssrc=42)

    packet, pcm = decoder._decode_packet("packet-marker")

    assert packet == "packet-marker"
    assert pcm == b"\x00" * discord.opus.Decoder.FRAME_SIZE


def test_other_exceptions_still_propagate():
    """Only an actual decode failure is swallowed - a genuinely different
    bug (a real programming error, not "this one packet was bad") must
    still surface rather than being silently hidden as well."""
    module = _fake_voice_recv_module(raise_on_decode=ValueError("boom"))
    apply_voice_recv_patches(module)
    decoder = module.opus.PacketDecoder(ssrc=1)

    with pytest.raises(ValueError):
        decoder._decode_packet("packet")


def test_patch_is_idempotent():
    module = _fake_voice_recv_module()
    apply_voice_recv_patches(module)
    patched_once = module.opus.PacketDecoder._decode_packet

    apply_voice_recv_patches(module)

    assert module.opus.PacketDecoder._decode_packet is patched_once


# ---------------------------------------------------------------- DAVE decryption


def test_dave_decrypt_replaces_the_payload_before_decode():
    session = _FakeDaveSession(decrypt_result=b"plaintext-opus")
    module = _fake_module_with_dave(dave_session=session, cached_id=123)
    apply_voice_recv_patches(module)
    decoder = module.opus.PacketDecoder(ssrc=5)
    packet = _FakePacket(b"mls-ciphertext")

    result_packet, pcm = decoder._decode_packet(packet)

    assert session.decrypt_calls == [(123, davey.MediaType.audio, b"mls-ciphertext")]
    assert result_packet.decrypted_data == b"plaintext-opus"
    assert pcm == b"decoded-plaintext-opus"


def test_dave_decrypt_skipped_when_there_is_no_session():
    """Non-DAVE calls (older discord.py, or a guild that hasn't rolled DAVE
    out) must be completely unaffected."""
    module = _fake_module_with_dave(dave_session=None)
    apply_voice_recv_patches(module)
    decoder = module.opus.PacketDecoder(ssrc=5)
    packet = _FakePacket(b"raw-opus")

    _, pcm = decoder._decode_packet(packet)

    assert packet.decrypted_data == b"raw-opus"
    assert pcm == b"decoded-raw-opus"


def test_dave_decrypt_skipped_when_session_not_ready():
    session = _FakeDaveSession(ready=False)
    module = _fake_module_with_dave(dave_session=session)
    apply_voice_recv_patches(module)
    decoder = module.opus.PacketDecoder(ssrc=5)

    decoder._decode_packet(_FakePacket(b"raw-opus"))

    assert session.decrypt_calls == []


def test_dave_decrypt_skipped_when_sender_not_yet_mapped():
    """`_cached_id` is only populated by `_process_packet`'s own
    member-resolution step, which runs *after* `_decode_packet` on a given
    SSRC's first call - decryption is a no-op until a later call carries
    the id over, rather than picking the wrong sender's ratchet."""
    session = _FakeDaveSession()
    module = _fake_module_with_dave(dave_session=session, cached_id=None)
    apply_voice_recv_patches(module)
    decoder = module.opus.PacketDecoder(ssrc=5)

    decoder._decode_packet(_FakePacket(b"raw-opus"))

    assert session.decrypt_calls == []


def test_dave_decrypt_failure_falls_through_to_decode_unchanged():
    """An unencrypted passthrough frame (silence/keepalive - DAVE
    explicitly allows these through in the clear) makes `decrypt()` raise;
    that's expected, so the frame must decode as-is rather than being
    dropped or crashing the pipeline."""
    session = _FakeDaveSession(raise_on_decrypt=RuntimeError("not E2EE"))
    module = _fake_module_with_dave(dave_session=session)
    apply_voice_recv_patches(module)
    decoder = module.opus.PacketDecoder(ssrc=5)
    packet = _FakePacket(b"passthrough-opus")

    result_packet, pcm = decoder._decode_packet(packet)

    assert result_packet.decrypted_data == b"passthrough-opus"
    assert pcm == b"decoded-passthrough-opus"


def test_dave_decrypt_and_opus_error_resilience_compose():
    """Both fixes share one patched call site - a DAVE-decrypted frame that
    *still* fails to decode (a genuine, separate packet-loss case) must
    still degrade to silence rather than crashing."""
    session = _FakeDaveSession(decrypt_result=b"plaintext-opus")
    module = _fake_module_with_dave(dave_session=session)
    module.opus.PacketDecoder._decode_packet = lambda self, packet: (_ for _ in ()).throw(_opus_error())
    apply_voice_recv_patches(module)
    decoder = module.opus.PacketDecoder(ssrc=5)
    packet = _FakePacket(b"mls-ciphertext")

    result_packet, pcm = decoder._decode_packet(packet)

    assert session.decrypt_calls == [(123, davey.MediaType.audio, b"mls-ciphertext")]
    assert result_packet.decrypted_data == b"plaintext-opus"
    assert pcm == b"\x00" * discord.opus.Decoder.FRAME_SIZE
