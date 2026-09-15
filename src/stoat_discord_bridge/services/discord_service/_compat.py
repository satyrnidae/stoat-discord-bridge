"""Runtime patches for discord-ext-voice-recv bugs the bridge trips over.

`discord-ext-voice-recv==0.5.2a179` (an alpha package, `pyproject.toml`'s
`voice` extra - issue #113 Phase 3) predates Discord's DAVE rollout and has
no per-packet error handling, so it trips over two separate problems in the
receive path - both patched here, at the same call site
(`opus.PacketDecoder._decode_packet`), since fixing the first exposes the
second:

1. **DAVE (End-to-End Encryption).** Discord voice calls are now
   end-to-end encrypted via DAVE (MLS-based), and this does *not*
   downgrade just because a receiving bot doesn't support it. voice_recv's
   `PacketDecryptor` only ever did the *transport*-level (SRTP) decryption
   - so after that succeeds, the Opus payload it hands to `_decode_packet`
   is still DAVE ciphertext, which either fails to decode
   (`discord.opus.OpusError: corrupted stream`, on essentially every
   packet) or - worse - occasionally "succeeds" into pure garbage/static.
   That is the actual explanation for "digital auditory vomit," not a
   codec/format mismatch: nothing was ever wrong with the PCM shape, the
   bytes handed to the decoder were simply still encrypted.

   discord.py >= 2.7 (with the optional `davey` package, confirmed
   installed here as part of the `voice` extra) already maintains a
   `DaveSession` on the voice connection to encrypt *outbound* audio -
   `_dave_decrypt` below reuses that same session to decrypt *inbound*
   frames per sender, right before decode, mirroring
   `discord-ext-voice-recv` upstream PR #58 (validated by its author
   against a real production bot: "before the patch, 100% of voice frames
   failed with corrupted stream; with it, full duplex voice works").
   `_cached_id` (the SSRC's resolved user id) is only ever populated by
   `_process_packet`'s own member-resolution step, which the unpatched
   code runs *after* calling `_decode_packet` - so DAVE decryption is a
   no-op for a given SSRC's very first packet (no sender identity yet to
   pick the right ratchet) but works from the second packet onward once
   `_cached_id` carries over from that first call. An unencrypted
   passthrough frame (silence/keepalives - DAVE explicitly allows these
   through in the clear) makes `session.decrypt(...)` raise; that's
   expected and just leaves the frame unchanged rather than being treated
   as a real failure.

2. **No per-packet error handling.** Even with DAVE decryption in place,
   an occasional genuinely undecodable frame (packet loss, a bad FEC
   decode - an ordinary, expected occurrence on a real UDP connection, not
   a bug) still raises `OpusError`, and voice_recv's `PacketRouter.run()`
   treats *any* such exception as fatal - it logs it and calls
   `stop_listening()` in its `finally` block, permanently ending audio
   receive for the *whole* connection (every SSRC), not just the one bad
   packet. Substituting a silent frame here instead, at the actual decode
   call site, means the jitter buffer / SSRC tracking / speaking-event
   state are all left untouched, and this common case never needs
   `DiscordVoiceTransport`'s separate restart-on-`after` logic
   (`services/discord_service/voice.py`) at all; that logic remains only
   as a backstop for a genuinely different, non-decode failure.

Idempotent; called from `_voice_recv_module()` once voice_recv's
availability is already confirmed (mirrors `stoat_service/_compat.py`'s
pattern for a comparable stoat.py bug).
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

_PATCH_FLAG = "_sdb_resilient_decode_packet_installed"


def apply_voice_recv_patches(voice_recv_module) -> None:
    """Make `voice_recv_module.opus.PacketDecoder._decode_packet` decrypt a
    DAVE-encrypted frame before decoding it, and substitute a silent frame
    for one that still fails to decode, rather than letting
    `discord.opus.OpusError` propagate and kill the whole receive
    connection. Safe to call more than once."""
    opus_module = voice_recv_module.opus
    decoder_cls = opus_module.PacketDecoder
    if getattr(decoder_cls, _PATCH_FLAG, False):
        return

    import discord.opus

    try:
        import davey
    except ImportError:
        davey = None

    original_decode_packet = decoder_cls._decode_packet
    silent_frame = b"\x00" * discord.opus.Decoder.FRAME_SIZE

    def _dave_decrypt(decoder, packet) -> None:
        if davey is None or not packet or not getattr(packet, "decrypted_data", None):
            return
        connection = getattr(decoder.sink.voice_client, "_connection", None)
        session = getattr(connection, "dave_session", None)
        if session is None or not session.ready or getattr(connection, "dave_protocol_version", 0) == 0:
            return
        user_id = decoder._cached_id
        if user_id is None:
            # SSRC not mapped to a sender yet - can't pick the right ratchet
            # without them; leave the frame for normal (probably-failing)
            # handling, same as upstream PR #58.
            return
        try:
            packet.decrypted_data = session.decrypt(
                int(user_id), davey.MediaType.audio, bytes(packet.decrypted_data)
            )
        except Exception as exc:
            # Expected for an unencrypted passthrough frame (silence/
            # keepalive); anything else still ends up decoded-or-dropped
            # below rather than crashing the router.
            logger.debug("[voice] DAVE decrypt failed for ssrc %s: %s", decoder.ssrc, exc)

    def _resilient_decode_packet(self, packet):
        try:
            _dave_decrypt(self, packet)
        except Exception:
            # Defense in depth on top of _dave_decrypt's own narrower guard -
            # an unexpected shape here (a discord.py internals change, a
            # test double missing an attribute) must never crash the whole
            # decode pipeline for what's ultimately a best-effort step.
            logger.debug("[voice] DAVE decrypt step failed for ssrc %s", self.ssrc, exc_info=True)
        try:
            return original_decode_packet(self, packet)
        except discord.opus.OpusError:
            logger.debug(
                "[voice] dropped one undecodable Opus packet (ssrc %s) - substituting silence rather than "
                "ending audio receive for the whole connection",
                self.ssrc,
            )
            return packet, silent_frame

    decoder_cls._decode_packet = _resilient_decode_packet
    setattr(decoder_cls, _PATCH_FLAG, True)
