"""Small helper for clipping a channel/category/role name to a destination
connector's per-entity name-length limit before a `/mirror` command hands it
to that connector's `ensure_*` hook (issue #99), plus the thread-group
Category title marker (issue #98).

Originally channel-only: fitting a Discord thread name into the 32-character
limit Stoat enforces on its own channel/category names, when mirroring a
Discord thread onto another connector. That call site
(`ChannelLinker.mirror_channel`, via `thread_category_title`) still relies on
the default limit.
"""

from __future__ import annotations

# Stoat channel/category/role names are all capped at 32 characters, which is
# also the tightest limit the bridge deals with - so it stays the default and
# the original Discord-thread-mirror call site is unaffected.
_DEFAULT_NAME_LIMIT = 32

# A bridge-generated thread-group Category is titled with this marker (thread
# emoji + the channel-identifier `#`) so it stands out from an ordinary
# same-named Category (issue #98). Applied at the one point every thread
# mirror funnels through (ChannelLinker.mirror_channel); stripped back off
# when matching a legacy thread Category to its parent channel by title
# (stoat_service group_parent_channel_with_threads). The mirrored thread
# *channel* names are left unprefixed - Stoat renders a client-side leading
# `#` on them already.
THREAD_CATEGORY_PREFIX = "🧵 #"

# A Discord *forum* channel mirrored as a Category (issue #100) is titled with
# this marker instead - speech-balloon emoji + the same `#` - so a forum group
# reads differently from an ordinary thread group (`🧵 #`). Same single
# insertion point (`ChannelLinker.mirror_channel` / `CategoryLinker.mirror_category`);
# `strip_thread_category_prefix` strips either marker.
FORUM_CATEGORY_PREFIX = "💬 #"


def clip_name(name: str, limit: int = _DEFAULT_NAME_LIMIT) -> str:
    """`name` stripped of surrounding whitespace and truncated to `limit`
    characters (default 32 - Stoat's cap)."""
    return name.strip()[:limit]


def thread_category_title(name: str) -> str:
    """Title for a bridge-generated thread-group Category: the parent channel
    name prefixed with `THREAD_CATEGORY_PREFIX` and clipped to the same limit
    a bare name is. The prefix is applied *before* the clip."""
    return clip_name(f"{THREAD_CATEGORY_PREFIX}{name.strip()}")


def forum_category_title(name: str) -> str:
    """Title for a Category mirrored from a Discord forum channel (issue #100):
    the forum name prefixed with `FORUM_CATEGORY_PREFIX` and clipped, exactly
    like `thread_category_title` but with the forum marker."""
    return clip_name(f"{FORUM_CATEGORY_PREFIX}{name.strip()}")


def strip_thread_category_prefix(title: str) -> str:
    """Inverse of `thread_category_title` / `forum_category_title` prefixing:
    drop a leading `THREAD_CATEGORY_PREFIX` or `FORUM_CATEGORY_PREFIX` from a
    Category title if present, so a legacy thread Category titled `🧵 #general`
    (or a forum Category `💬 #general`) still matches parent channel `general`.
    A title without either prefix is returned unchanged."""
    for prefix in (THREAD_CATEGORY_PREFIX, FORUM_CATEGORY_PREFIX):
        if title.startswith(prefix):
            return title[len(prefix) :]
    return title
