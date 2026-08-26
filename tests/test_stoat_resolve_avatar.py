"""Tests for StoatSenderService._resolve_avatar_url - the fix for a real bug:
a Member's underlying User isn't always cached by the time its message
arrives (the bot hasn't chunked that member yet), so the avatar reads as
unset even when the sender has a real one, and every relay used the
platform's generic default avatar instead.

Constructs the service via object.__new__ rather than StoatSenderService(...)
directly, since __init__ builds a _StoatClient whose constructor makes a real
network call (_discover_websocket_base, covered separately in
test_stoat_websocket_discovery.py) - _resolve_avatar_url only touches
self._client, so a fake stand-in is enough.
"""

from __future__ import annotations

from types import SimpleNamespace

from stoat_discord_bridge.services.stoat_service import StoatSenderService


def _asset(url: str):
    return SimpleNamespace(url=lambda: url)


def _make_sender(client) -> StoatSenderService:
    sender = object.__new__(StoatSenderService)
    sender._client = client
    return sender


async def test_uses_the_cached_avatar_without_fetching():
    author = SimpleNamespace(
        id="u1",
        server_avatar=None,
        avatar=_asset("https://cdn.example/account.png"),
        default_avatar_url="https://cdn.example/default.png",
    )
    message = SimpleNamespace(author=author, channel=SimpleNamespace(server_id="s1"))

    class FailingClient:
        def get_server(self, *args, **kwargs):
            raise AssertionError("should not fetch when the avatar is already cached")

    sender = _make_sender(FailingClient())
    assert await sender._resolve_avatar_url(message) == "https://cdn.example/account.png"


async def test_fetches_a_fresh_member_when_avatar_is_uncached_in_a_server_channel():
    author = SimpleNamespace(
        id="u1", server_avatar=None, avatar=None, default_avatar_url="https://cdn.example/default.png"
    )
    fresh_member = SimpleNamespace(
        server_avatar=_asset("https://cdn.example/fresh.png"),
        avatar=None,
        default_avatar_url="https://cdn.example/fresh-default.png",
    )
    fetch_calls = []

    class FakeServer:
        async def fetch_member(self, user_id):
            fetch_calls.append(user_id)
            return fresh_member

    class FakeClient:
        def get_server(self, server_id, *, partial=True):
            assert server_id == "s1"
            assert partial is True
            return FakeServer()

    message = SimpleNamespace(author=author, channel=SimpleNamespace(server_id="s1"))
    sender = _make_sender(FakeClient())

    assert await sender._resolve_avatar_url(message) == "https://cdn.example/fresh.png"
    assert fetch_calls == ["u1"]


async def test_fetches_a_fresh_user_for_a_dm_channel():
    author = SimpleNamespace(
        id="u1", server_avatar=None, avatar=None, default_avatar_url="https://cdn.example/default.png"
    )
    fresh_user = SimpleNamespace(avatar=_asset("https://cdn.example/fresh-dm.png"))

    class FakeClient:
        def get_server(self, *args, **kwargs):
            raise AssertionError("a DM channel has no server to fetch a member from")

        async def fetch_user(self, user_id):
            assert user_id == "u1"
            return fresh_user

    message = SimpleNamespace(author=author, channel=SimpleNamespace())  # no server_id attribute
    sender = _make_sender(FakeClient())

    assert await sender._resolve_avatar_url(message) == "https://cdn.example/fresh-dm.png"


async def test_falls_back_to_the_cached_default_when_the_fetch_raises():
    author = SimpleNamespace(
        id="u1", server_avatar=None, avatar=None, default_avatar_url="https://cdn.example/default.png"
    )

    class FakeServer:
        async def fetch_member(self, user_id):
            raise RuntimeError("network error")

    class FakeClient:
        def get_server(self, *args, **kwargs):
            return FakeServer()

    message = SimpleNamespace(author=author, channel=SimpleNamespace(server_id="s1"))
    sender = _make_sender(FakeClient())

    assert await sender._resolve_avatar_url(message) == "https://cdn.example/default.png"


async def test_falls_back_to_the_fresh_objects_default_when_it_also_has_no_avatar():
    author = SimpleNamespace(
        id="u1", server_avatar=None, avatar=None, default_avatar_url="https://cdn.example/default.png"
    )
    fresh_member = SimpleNamespace(
        server_avatar=None, avatar=None, default_avatar_url="https://cdn.example/fresh-default.png"
    )

    class FakeServer:
        async def fetch_member(self, user_id):
            return fresh_member

    class FakeClient:
        def get_server(self, *args, **kwargs):
            return FakeServer()

    message = SimpleNamespace(author=author, channel=SimpleNamespace(server_id="s1"))
    sender = _make_sender(FakeClient())

    assert await sender._resolve_avatar_url(message) == "https://cdn.example/fresh-default.png"
