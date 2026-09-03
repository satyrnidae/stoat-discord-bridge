"""A tiny async TTL cache.

Used by the Discord and Stoat senders to memoise best-effort per-user
pronoun lookups so a profile endpoint isn't hit on every relayed message.
A `None` result is cached like any other (a user with no pronouns set
shouldn't be re-fetched each message); the TTL is what lets a pronoun set
later still show up, eventually.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from typing import Generic, TypeVar

_V = TypeVar("_V")


class AsyncTTLCache(Generic[_V]):
    def __init__(self, ttl_seconds: float) -> None:
        self._ttl = ttl_seconds
        self._entries: dict[str, tuple[_V, float]] = {}

    async def get(self, key: str, loader: Callable[[str], Awaitable[_V]]) -> _V:
        """Return the cached value for `key`, or `await loader(key)` and cache
        it. `loader` is only awaited on a miss or an expired entry."""
        now = time.monotonic()
        hit = self._entries.get(key)
        if hit is not None and hit[1] > now:
            return hit[0]
        value = await loader(key)
        self._entries[key] = (value, now + self._ttl)
        self._maybe_sweep(now)
        return value

    def _maybe_sweep(self, now: float) -> None:
        # Entries are only refreshed on re-access, so a one-off lookup would
        # otherwise sit in the dict forever. Drop everything expired once the
        # dict grows past a small threshold - cheap, and bounded by how many
        # distinct users are seen within one TTL window.
        if len(self._entries) <= 256:
            return
        self._entries = {k: v for k, v in self._entries.items() if v[1] > now}
