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


def clip_name(name: str, limit: int = _DEFAULT_NAME_LIMIT) -> str:
    """`name` stripped of surrounding whitespace and truncated to `limit`
    characters (default 32 - Stoat's cap)."""
    return name.strip()[:limit]


def thread_category_title(name: str) -> str:
    """Title for a bridge-generated thread-group Category: the parent channel
    name prefixed with `THREAD_CATEGORY_PREFIX` and clipped to the same limit
    a bare name is. The prefix is applied *before* the clip."""
    return clip_name(f"{THREAD_CATEGORY_PREFIX}{name.strip()}")


def strip_thread_category_prefix(title: str) -> str:
    """Inverse of `thread_category_title`'s prefixing: drop a leading
    `THREAD_CATEGORY_PREFIX` from a Category title if present, so a legacy
    thread Category titled `🧵 #general` still matches parent channel
    `general`. A title without the prefix is returned unchanged."""
    if title.startswith(THREAD_CATEGORY_PREFIX):
        return title[len(THREAD_CATEGORY_PREFIX) :]
    return title
