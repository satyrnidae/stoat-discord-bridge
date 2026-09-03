"""Sender-side pronoun resolution for issue #54.

Neither discord.py 2.7.1 nor stoat.py 1.2.1 models a pronoun field, so each
sender reads it best-effort off a raw profile request. These tests cover the
raw-payload parsing, the per-server-before-account preference, the
`pronoun_forwarding` gate, and the per-user cache - without any network.
"""

from __future__ import annotations

from collections import deque
from types import SimpleNamespace

import pytest

from stoat_discord_bridge.config import DiscordConnectorConfig
from stoat_discord_bridge.services.caching import AsyncTTLCache
from stoat_discord_bridge.services.discord_service import DiscordSenderService
from stoat_discord_bridge.services.stoat_service import StoatSenderService
from stoat_discord_bridge.services.stoat_service.formatting import _extract_pronouns
from stoat_discord_bridge.status import HealthTracker


# ----------------------------------------------------------------- AsyncTTLCache


async def test_ttl_cache_only_calls_the_loader_once_per_key():
    calls: list[str] = []

    async def loader(key: str) -> str:
        calls.append(key)
        return f"v-{key}"

    cache: AsyncTTLCache[str] = AsyncTTLCache(600.0)
    assert await cache.get("a", loader) == "v-a"
    assert await cache.get("a", loader) == "v-a"
    assert await cache.get("b", loader) == "v-b"
    assert calls == ["a", "b"]


async def test_ttl_cache_re_fetches_after_expiry(monkeypatch):
    import stoat_discord_bridge.services.caching as caching_mod

    clock = {"now": 1000.0}
    monkeypatch.setattr(caching_mod.time, "monotonic", lambda: clock["now"])
    calls: list[str] = []

    async def loader(_key: str) -> str:
        calls.append(_key)
        return "x"

    cache: AsyncTTLCache[str] = AsyncTTLCache(10.0)
    await cache.get("k", loader)
    clock["now"] += 5
    await cache.get("k", loader)  # still fresh
    clock["now"] += 20
    await cache.get("k", loader)  # expired
    assert calls == ["k", "k"]


# ----------------------------------------------------------------- Stoat: _extract_pronouns


@pytest.mark.parametrize(
    "payload, expected",
    [
        ({"pronouns": "she/her"}, "she/her"),
        ({"pronouns": "  they/them  "}, "they/them"),
        ({"profile": {"pronouns": "he/him"}}, "he/him"),
        ({"pronouns": "", "profile": {"pronouns": "xe/xem"}}, "xe/xem"),
        ({"nickname": "x"}, None),
        ({"profile": "not-a-dict"}, None),
        ("not-a-dict", None),
        ({}, None),
    ],
)
def test_extract_pronouns(payload, expected):
    assert _extract_pronouns(payload) == expected


# ----------------------------------------------------------------- Discord sender


class _FakeDiscordHTTP:
    def __init__(self, response=None, raises: BaseException | None = None) -> None:
        self._response = response
        self._raises = raises
        self.calls: list = []

    async def request(self, route, **kwargs):
        self.calls.append((route.method, route.url, kwargs.get("params")))
        if self._raises is not None:
            raise self._raises
        return self._response


def _discord_sender(http, *, pronoun_forwarding: bool = True) -> DiscordSenderService:
    sender = DiscordSenderService(
        DiscordConnectorConfig(
            id="discord", label="Discord", guild_id=123, bot_token="t", pronoun_forwarding=pronoun_forwarding
        ),
        on_message=_noop,
        health=HealthTracker({"discord": "Discord"}),
    )
    sender._client = SimpleNamespace(http=http)
    return sender


async def _noop(_message) -> None:
    pass


async def test_discord_prefers_the_guild_member_profile_pronouns():
    http = _FakeDiscordHTTP(
        {"user_profile": {"pronouns": "she/her"}, "guild_member_profile": {"pronouns": "she/they"}}
    )
    sender = _discord_sender(http)

    assert await sender._resolve_sender_pronouns(555) == "she/they"
    assert http.calls[0][2] == {"guild_id": "123", "with_mutual_guilds": "false"}


async def test_discord_falls_back_to_the_account_profile_pronouns():
    http = _FakeDiscordHTTP({"user_profile": {"pronouns": "she/her"}, "guild_member_profile": {}})
    sender = _discord_sender(http)

    assert await sender._resolve_sender_pronouns(555) == "she/her"


async def test_discord_no_pronouns_anywhere_is_none():
    sender = _discord_sender(_FakeDiscordHTTP({"user_profile": {}, "guild_member_profile": {}}))
    assert await sender._resolve_sender_pronouns(555) is None


async def test_discord_a_raising_profile_request_is_swallowed():
    sender = _discord_sender(_FakeDiscordHTTP(raises=RuntimeError("429")))
    assert await sender._resolve_sender_pronouns(555) is None


async def test_discord_pronoun_forwarding_off_skips_the_request_entirely():
    http = _FakeDiscordHTTP({"user_profile": {"pronouns": "she/her"}})
    sender = _discord_sender(http, pronoun_forwarding=False)

    assert await sender._resolve_sender_pronouns(555) is None
    assert http.calls == []


async def test_discord_pronouns_are_cached_per_user():
    http = _FakeDiscordHTTP({"guild_member_profile": {"pronouns": "she/her"}})
    sender = _discord_sender(http)

    await sender._resolve_sender_pronouns(555)
    await sender._resolve_sender_pronouns(555)

    assert len(http.calls) == 1


# ----------------------------------------------------------------- Stoat sender


class _FakeStoatHTTP:
    def __init__(self, responses: dict[str, object]) -> None:
        # keyed by the route's trailing path shape we care about
        self._responses = responses
        self.calls: list[str] = []

    async def request(self, route, **kwargs):
        path = route.build()
        self.calls.append(path)
        for needle, value in self._responses.items():
            if needle in path:
                if isinstance(value, BaseException):
                    raise value
                return value
        raise RuntimeError(f"no fake response for {path}")


def _stoat_sender(http, *, pronoun_forwarding: bool = True) -> StoatSenderService:
    sender = object.__new__(StoatSenderService)
    sender.connector_id = "stoat"
    sender.server_id = "srv-1"
    sender._client = SimpleNamespace(http=http)
    sender._config = SimpleNamespace(pronoun_forwarding=pronoun_forwarding)
    sender._pronoun_cache = AsyncTTLCache(600.0)
    return sender


def _stoat_message(server_id: str | None = "srv-1", author_id: str = "u1"):
    return SimpleNamespace(author=SimpleNamespace(id=author_id), channel=SimpleNamespace(server_id=server_id))


async def test_stoat_prefers_the_server_member_pronouns_over_the_account():
    http = _FakeStoatHTTP(
        {"/members/": {"pronouns": "she/they"}, "/users/u1": {"pronouns": "she/her"}}
    )
    sender = _stoat_sender(http)

    assert await sender._resolve_sender_pronouns(_stoat_message()) == "she/they"
    assert http.calls[0].endswith("/members/u1")


async def test_stoat_falls_through_member_then_user_then_profile():
    http = _FakeStoatHTTP(
        {
            "/members/": {},
            "/users/u1/profile": {"pronouns": "he/him"},
            "/users/u1": {},
        }
    )
    sender = _stoat_sender(http)

    assert await sender._resolve_sender_pronouns(_stoat_message()) == "he/him"


async def test_stoat_a_raising_source_is_skipped_for_the_next():
    http = _FakeStoatHTTP(
        {"/members/": RuntimeError("boom"), "/users/u1/profile": {}, "/users/u1": {"pronouns": "they/them"}}
    )
    sender = _stoat_sender(http)

    assert await sender._resolve_sender_pronouns(_stoat_message()) == "they/them"


async def test_stoat_dm_channel_with_no_server_id_uses_the_connector_server():
    http = _FakeStoatHTTP({"/members/": {"pronouns": "she/her"}, "/users/u1": {}})
    sender = _stoat_sender(http)

    assert await sender._resolve_sender_pronouns(_stoat_message(server_id=None)) == "she/her"
    assert "srv-1/members/u1" in http.calls[0]


async def test_stoat_pronoun_forwarding_off_skips_the_request():
    http = _FakeStoatHTTP({"/members/": {"pronouns": "she/her"}})
    sender = _stoat_sender(http, pronoun_forwarding=False)

    assert await sender._resolve_sender_pronouns(_stoat_message()) is None
    assert http.calls == []
