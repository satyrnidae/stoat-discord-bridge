"""`StoatSenderService.refresh()` - the `/mirror` full-refresh hook (issue
#81).

stoat.py 1.2.1 populates the cached `Server` once at gateway connect and
only patches it from a narrow set of events, so a channel / role / emoji
created since then is invisible to the cache-only `resolve_* / ensure_* /
list_*` readers - and `/mirror` would then spawn a duplicate. `refresh()`
re-fetches the server and writes it (plus members and emoji) back into
stoat.py's cache; these tests use a stand-in cache to pin that the
cache-backed readers see the fresh entities afterwards, and that the
`RefreshThrottle` collapses a fan-out burst to one fetch.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from stoat_discord_bridge.services.stoat_service import StoatSenderService

pytestmark = pytest.mark.asyncio


class _FakeCache:
    def __init__(self) -> None:
        self._servers: dict[str, object] = {}
        self._channels: dict[str, object] = {}
        self._members: dict[str, dict[str, object]] = {}
        self._emojis: dict[str, object] = {}

    def store_server(self, server, ctx, /) -> None:
        self._servers[server.id] = server

    def store_channel(self, channel, ctx, /) -> None:
        self._channels[channel.id] = channel

    def overwrite_server_members(self, server_id, members, ctx, /) -> None:
        self._members[server_id] = dict(members)

    def store_emoji(self, emoji, ctx, /) -> None:
        self._emojis[str(emoji.id)] = emoji

    def get_server(self, server_id):
        return self._servers.get(server_id)

    def get_channel(self, channel_id):
        return self._channels.get(channel_id)


class _FakeServer:
    def __init__(self, id: str, *, roles=(), channels=(), emojis=(), members=()) -> None:
        self.id = id
        self.roles = list(roles)
        self._channels = list(channels)
        self.categories = []
        self._emojis = {str(e.id): e for e in emojis}
        self._members = list(members)
        self.internal_channels = (False, list(channels))
        self.fetch_members_calls = 0
        self.fetch_emojis_calls = 0

    @property
    def emojis(self):
        return dict(self._emojis)

    def prepare_cached(self):
        if self.internal_channels[0]:
            return []
        chans = self.internal_channels[1]
        self.internal_channels = (True, [c.id for c in chans])
        return chans

    async def fetch_members(self, **kwargs):
        self.fetch_members_calls += 1
        return list(self._members)

    async def fetch_emojis(self, **kwargs):
        self.fetch_emojis_calls += 1
        return list(self._emojis.values())


class _FakeClient:
    def __init__(self, *, cached: _FakeServer, fresh: _FakeServer) -> None:
        self._cache = _FakeCache()
        self._cache.store_server(cached, None)
        self._fresh = fresh
        self.state = SimpleNamespace(cache=self._cache)
        self.fetch_server_calls = 0

    async def fetch_server(self, server_id: str, *, populate_channels: bool = False):
        self.fetch_server_calls += 1
        return self._fresh

    def get_server(self, server_id: str, *, partial: bool = False):
        return self._cache.get_server(server_id)

    def get_channel(self, channel_id: str, *, partial: bool = False):
        return self._cache.get_channel(channel_id)


def _role(id: str, name: str):
    return SimpleNamespace(id=id, name=name)


def _sender(client: _FakeClient) -> StoatSenderService:
    sender = object.__new__(StoatSenderService)
    sender.connector_id = "stoat"
    sender.server_id = "s1"
    sender._client = client
    return sender


async def test_refresh_makes_a_new_role_resolvable():
    cached = _FakeServer("s1", roles=[_role("r1", "Mods")])
    fresh = _FakeServer("s1", roles=[_role("r1", "Mods"), _role("r2", "Admins")])
    sender = _sender(_FakeClient(cached=cached, fresh=fresh))

    assert await sender.resolve_role_id_by_name("Admins") is None  # stale cache misses it
    await sender.refresh()
    assert await sender.resolve_role_id_by_name("Admins") == "r2"


async def test_refresh_makes_a_new_channel_and_emoji_visible():
    new_channel = SimpleNamespace(id="c9", name="new-chan")
    new_emoji = SimpleNamespace(id="e9", name="blob", animated=False)
    cached = _FakeServer("s1")
    fresh = _FakeServer("s1", channels=[new_channel], emojis=[new_emoji])
    sender = _sender(_FakeClient(cached=cached, fresh=fresh))

    await sender.refresh()

    assert await sender.get_channel_name("c9") == "new-chan"
    assert await sender.resolve_emoji_id_by_name("blob") == "e9"
    assert fresh.fetch_members_calls == 1
    assert fresh.fetch_emojis_calls == 1


async def test_refresh_is_throttled_within_the_interval():
    cached = _FakeServer("s1")
    fresh = _FakeServer("s1")
    client = _FakeClient(cached=cached, fresh=fresh)
    sender = _sender(client)

    await sender.refresh()
    await sender.refresh()
    await sender.refresh()

    assert client.fetch_server_calls == 1  # the burst collapsed to one fetch


async def test_a_failing_fetch_server_does_not_burn_the_throttle_window():
    calls = {"n": 0}

    class _Flaky(_FakeClient):
        async def fetch_server(self, server_id, *, populate_channels=False):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("network down")
            return self._fresh

    sender = _sender(_Flaky(cached=_FakeServer("s1"), fresh=_FakeServer("s1", roles=[_role("r2", "Admins")])))

    await sender.refresh()  # first attempt fails, must not raise
    await sender.refresh()  # immediately retries rather than being throttled

    assert calls["n"] == 2
    assert await sender.resolve_role_id_by_name("Admins") == "r2"
