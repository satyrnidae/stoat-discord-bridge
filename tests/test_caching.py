"""`services/caching.AsyncTTLCache` - the tiny async memo behind per-user
pronoun lookups and (issue #66) the Stoat Category-list re-fetch."""

from __future__ import annotations

import pytest

from stoat_discord_bridge.services.caching import AsyncTTLCache

pytestmark = pytest.mark.asyncio


async def test_loader_runs_once_then_the_value_is_cached():
    calls: list[str] = []

    async def loader(key: str) -> str:
        calls.append(key)
        return f"v:{key}"

    cache: AsyncTTLCache[str] = AsyncTTLCache(ttl_seconds=60)

    assert await cache.get("a", loader) == "v:a"
    assert await cache.get("a", loader) == "v:a"
    assert calls == ["a"]


async def test_invalidate_forces_the_next_get_to_reload():
    calls: list[str] = []

    async def loader(key: str) -> int:
        calls.append(key)
        return len(calls)

    cache: AsyncTTLCache[int] = AsyncTTLCache(ttl_seconds=60)

    assert await cache.get("k", loader) == 1
    cache.invalidate("k")
    assert await cache.get("k", loader) == 2  # reloaded, not served from cache
    assert await cache.get("k", loader) == 2  # cached again


async def test_invalidate_is_a_noop_for_an_unknown_key():
    cache: AsyncTTLCache[str] = AsyncTTLCache(ttl_seconds=60)

    cache.invalidate("missing")  # must not raise


async def test_an_expired_entry_reloads():
    calls: list[str] = []

    async def loader(key: str) -> str:
        calls.append(key)
        return key

    cache: AsyncTTLCache[str] = AsyncTTLCache(ttl_seconds=0)

    await cache.get("x", loader)
    await cache.get("x", loader)
    assert calls == ["x", "x"]  # ttl=0 -> every get is a miss
