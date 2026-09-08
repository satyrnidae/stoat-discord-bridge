"""Small helper for fitting a Discord channel/category name into the
32-character limit Stoat enforces on its own channel/category names, used
when mirroring a Discord thread onto another connector.
"""

from __future__ import annotations

# Stoat category/channel names are capped at 32 characters.
_NAME_LIMIT = 32

# A bridge-generated thread-group Category is titled with this marker (thread
# emoji + the channel-identifier `#`) so it stands out from an ordinary
# same-named Category (issue #98). Applied at the one point every thread
# mirror funnels through (ChannelLinker.mirror_channel); stripped back off
# when matching a legacy thread Category to its parent channel by title
# (stoat_service group_parent_channel_with_threads). The mirrored thread
# *channel* names are left unprefixed - Stoat renders a client-side leading
# `#` on them already.
THREAD_CATEGORY_PREFIX = "🧵 #"


def clip_name(name: str) -> str:
    return name.strip()[:_NAME_LIMIT]


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
