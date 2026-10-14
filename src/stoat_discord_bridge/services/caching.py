"""A tiny async TTL cache plus a refresh throttle.

`AsyncTTLCache` is used by the Discord and Stoat senders to memoise
best-effort per-user pronoun lookups so a profile endpoint isn't hit on
every relayed message, and by the Stoat sender for its short-TTL Category
re-fetch. A `None` result is cached like any other (a user with no pronouns
set shouldn't be re-fetched each message); the TTL is what lets a pronoun
set later still show up, eventually.

`RefreshThrottle` collapses a burst of "re-fetch the whole server" calls
(the `/mirror` full-refresh, issue #81) down to one network round-trip.
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

    def invalidate(self, key: str) -> None:
        """Drop the cached entry for `key` so the next `get` re-runs `loader`.
        A no-op if nothing is cached under it."""
        self._entries.pop(key, None)

    def put(self, key: str, value: _V) -> None:
        """Seed the cache for `key` directly, as if `loader` had just returned
        `value` - it then lives out a full TTL like any other entry. Used when
        a caller has already fetched fresh data by another route (Stoat's
        `refresh()`, issue #81) and wants a following `get` served from it
        rather than re-fetching."""
        now = time.monotonic()
        self._entries[key] = (value, now + self._ttl)
        self._maybe_sweep(now)

    def _maybe_sweep(self, now: float) -> None:
        # Entries are only refreshed on re-access, so a one-off lookup would
        # otherwise sit in the dict forever. Drop everything expired once the
        # dict grows past a small threshold - cheap, and bounded by how many
        # distinct users are seen within one TTL window.
        if len(self._entries) <= 256:
            return
        self._entries = {k: v for k, v in self._entries.items() if v[1] > now}


class RefreshThrottle:
    """Guards an expensive "re-fetch everything from the API" call so a burst
    of them collapses to one network round-trip.

    A `/mirror <noun> all` fan-out (and `/mirror category`, which re-enters
    `mirror_channel` per child) would otherwise force a full server re-fetch
    per destination per child. `due()` is a pure check - it returns False for
    `min_interval` seconds after the last `mark()`; the caller runs the fetch
    and only `mark()`s once it actually succeeds, so a failed fetch doesn't
    burn the window and the next attempt retries immediately.

    The window spans the whole service instance, not one command, so two
    distinct `/mirror` commands issued inside `min_interval` share it - the
    second reads the (just-refreshed) cache without re-fetching. That's the
    intended trade: the fan-out saving is worth far more than catching an
    entity created in that sub-`min_interval` gap between two commands.
    """

    def __init__(self, min_interval: float) -> None:
        self._min_interval = min_interval
        self._last = float("-inf")

    def due(self) -> bool:
        return time.monotonic() - self._last >= self._min_interval

    def mark(self) -> None:
        """Record that a refresh just completed - `due()` then returns False
        until `min_interval` seconds have passed."""
        self._last = time.monotonic()
