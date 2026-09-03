"""Small helper for fitting a channel/category/role name into the
32-character limit Stoat enforces on its own channel/category/role names,
used whenever the bridge creates a Stoat entity from a name that originated
on another connector - Discord thread mirroring and the `/mirror` admin
commands both feed names through here.
"""

from __future__ import annotations

# Stoat channel / category / role names are capped at 32 characters.
_NAME_LIMIT = 32


def clip_name(name: str) -> str:
    return name.strip()[:_NAME_LIMIT]
