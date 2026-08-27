"""Tests for StoatSenderService._resolve_sender_name - the name-side
counterpart of the bug test_stoat_resolve_avatar.py covers for avatars: a
Member's underlying User isn't always cached by the time its message arrives
(the bot hasn't chunked that member yet), so stoat.py's Member.name/
display_name properties silently return ""/None rather than the real value,
and every relay would show a blank sender name instead.

Constructs the service via object.__new__ rather than StoatSenderService(...)
directly, since __init__ builds a _StoatClient whose constructor makes a real
network call (_discover_websocket_base, covered separately in
test_stoat_websocket_discovery.py) - _resolve_sender_name only touches
self._client, so a fake stand-in is enough.
"""

from __future__ import annotations

from types import SimpleNamespace

from stoat_discord_bridge.services.stoat_service import StoatSenderService


def _make_sender(client) -> StoatSenderService:
    sender = object.__new__(StoatSenderService)
    sender._client = client
    return sender


async def test_uses_the_cached_name_without_fetching():
    author = SimpleNamespace(id="u1", nick=None, display_name=None, name="alice")
    message = SimpleNamespace(author=author, channel=SimpleNamespace(server_id="s1"))

    class FailingClient:
        def get_server(self, *args, **kwargs):
            raise AssertionError("should not fetch when the name is already cached")

    sender = _make_sender(FailingClient())
    assert await sender._resolve_sender_name(message) == "alice"


async def test_fetches_a_fresh_member_when_name_is_uncached_in_a_server_channel():
    author = SimpleNamespace(id="u1", nick=None, display_name=None, name="")
    fresh_member = SimpleNamespace(nick=None, display_name="Fresh Alice", name="alice")
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

    assert await sender._resolve_sender_name(message) == "Fresh Alice"
    assert fetch_calls == ["u1"]


async def test_fetches_a_fresh_user_for_a_dm_channel():
    author = SimpleNamespace(id="u1", nick=None, display_name=None, name="")
    fresh_user = SimpleNamespace(nick=None, display_name=None, name="alice")

    class FakeClient:
        def get_server(self, *args, **kwargs):
            raise AssertionError("a DM channel has no server to fetch a member from")

        async def fetch_user(self, user_id):
            assert user_id == "u1"
            return fresh_user

    message = SimpleNamespace(author=author, channel=SimpleNamespace())  # no server_id attribute
    sender = _make_sender(FakeClient())

    assert await sender._resolve_sender_name(message) == "alice"


async def test_falls_back_to_the_cached_empty_name_when_the_fetch_raises():
    author = SimpleNamespace(id="u1", nick=None, display_name=None, name="")

    class FakeServer:
        async def fetch_member(self, user_id):
            raise RuntimeError("network error")

    class FakeClient:
        def get_server(self, *args, **kwargs):
            return FakeServer()

    message = SimpleNamespace(author=author, channel=SimpleNamespace(server_id="s1"))
    sender = _make_sender(FakeClient())

    assert await sender._resolve_sender_name(message) == ""


async def test_falls_back_to_the_cached_empty_name_when_the_fresh_object_also_has_none():
    author = SimpleNamespace(id="u1", nick=None, display_name=None, name="")
    fresh_member = SimpleNamespace(nick=None, display_name=None, name="")

    class FakeServer:
        async def fetch_member(self, user_id):
            return fresh_member

    class FakeClient:
        def get_server(self, *args, **kwargs):
            return FakeServer()

    message = SimpleNamespace(author=author, channel=SimpleNamespace(server_id="s1"))
    sender = _make_sender(FakeClient())

    assert await sender._resolve_sender_name(message) == ""
