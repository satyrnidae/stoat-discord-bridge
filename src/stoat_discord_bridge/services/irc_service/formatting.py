"""Plain-text / line-limit helpers for the IRC connector.

IRC carries no markup, no native attachments, and no message IDs, so the
receiver reduces everything to plain text, splits on a byte budget, and
synthesises a stable per-message key. These pieces are network-free and
shared by the sender and receiver halves.
"""

from __future__ import annotations

import hashlib
import re
import time

# IRC's protocol limit is 512 bytes per raw line, including the
# `:nick!user@host PRIVMSG #channel :` prefix the server sees and the
# trailing CRLF. We don't know our own hostmask as the server will render it,
# so this leaves a generous margin for that plus the "<sender_name> " prefix
# this receiver prepends to each line.
_LINE_LIMIT = 400

# InspIRCd's +P keeps a channel (and its modes/topic/bans) alive on the
# server even while it's empty, so a synced channel mirrored here doesn't
# evaporate between messages. It's an oper-only mode, so it can only be set
# once our OPER handshake (_handle_welcome) has been confirmed by the server
# (RPL_YOUREOPER -> on_youreoper); a synced channel created before then is
# parked in _pending_permanent_modes and gets +P the moment we're opered.
# Applied only when `P` is one of the connector's configured
# default_channel_modes, and never to Discord-thread channels
# (ensure_channel's is_thread_category) - threads are ephemeral, so their
# mirror channel should be free to disappear when empty like any other.
_PERMANENT_CHANNEL_MODE = "+P"

# irc.satyrn.dev (InspIRCd-4 + a chanhistory-style module, enabled via the
# `H` in default_channel_modes) replays recent history to a channel right
# after JOIN, announced by a channel NOTICE of this exact shape (confirmed
# by a live probe against the real server - not a guess):
#   "Replaying up to 50 lines of pre-join history from the last ..."
# immediately followed by that many PRIVMSGs, indistinguishable from live
# traffic otherwise. This network doesn't support CAP negotiation at all
# (CAP LS gets 421 Unknown command), so there's no IRCv3 batch/server-time
# to lean on, and there's no explicit "end of replay" marker either - only
# the server's own "up to N" cap on how many lines *could* follow.
_HISTORY_REPLAY_NOTICE_RE = re.compile(r"Replaying up to (\d+) lines? of pre-join history", re.IGNORECASE)
# Safety net for a channel whose actual history was shorter than the
# announced cap: without this, the leftover "budget" would silently
# swallow the next several genuinely-live messages whenever they next
# arrive, however much later. Observed replay bursts land in a single TCP
# read (sub-second), so this is generous, not tight.
_HISTORY_REPLAY_TIMEOUT = 5.0


def _split_permanent_mode(modes: str) -> tuple[str | None, bool]:
    """Split a MODE string like `+HtnPR` into (`+HtnR`, True) - the modes to
    apply to a channel immediately on creation, and whether `P` (handled
    separately, oper-gated) was among them. Returns (None, ...) when nothing
    but a bare sign is left."""
    had_p = "P" in modes
    if not had_p:
        return modes, False
    stripped = modes.replace("P", "")
    return (None if stripped.strip(" +-") == "" else stripped), True


def _synthetic_message_id(channel: str, nick: str, content: str) -> str:
    # IRC has no native message IDs. Hash the message contents (scoped by
    # channel/nick, salted with receipt time to keep repeated identical
    # messages from colliding) so sync tracking has a stable per-message key.
    digest = hashlib.sha256(f"{channel}\0{nick}\0{content}\0{time.time_ns()}".encode()).hexdigest()
    return f"irc-{digest[:16]}"
