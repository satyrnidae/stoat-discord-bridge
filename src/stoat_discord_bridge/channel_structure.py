"""Small helper for fitting a Discord channel/category name into the
32-character limit Stoat enforces on its own channel/category names, used
when mirroring a Discord thread onto another connector.
"""

from __future__ import annotations

# Stoat category/channel names are capped at 32 characters.
_NAME_LIMIT = 32


def clip_name(name: str) -> str:
    return name.strip()[:_NAME_LIMIT]
