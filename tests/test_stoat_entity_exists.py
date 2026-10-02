"""StoatSenderService.entity_exists - the strict existence check a stale
link is pruned on (issue #217). Only a stoat.NotFound (or a role missing from
a freshly fetched server) counts as gone; anything else is "can't tell"."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from stoat_discord_bridge.services.stoat_service import StoatSenderService
from tests.fakes.fake_stoat import (
    FakeChannel,
    FakeClient,
    FakeEmoji,
    FakeServer,
    stoat_forbidden,
    stoat_not_found,
)

pytestmark = pytest.mark.asyncio


def _sender(client: FakeClient, server: FakeServer) -> StoatSenderService:
    client.add_server(server)
    sender = object.__new__(StoatSenderService)
    sender.connector_id = "stoat"
    sender._client = client
    sender.server_id = server.id
    return sender


def _raising(exc: BaseException):
    async def fetch(*_args, **_kwargs):
        raise exc

    return fetch


async def test_cached_channel_exists():
    client = FakeClient()
    client.add_channel(FakeChannel(id="c1", name="general", server_id="s1"))
    sender = _sender(client, FakeServer("s1"))

    assert await sender.entity_exists("channel", "c1") is True


async def test_uncached_channel_falls_back_to_a_fetch():
    client = FakeClient()
    client.set_fetched_channel(FakeChannel(id="c1", name="general", server_id="s1"))
    sender = _sender(client, FakeServer("s1"))

    assert await sender.entity_exists("channel", "c1") is True


async def test_channel_that_404s_is_gone():
    client = FakeClient()
    client.fetch_channel = _raising(stoat_not_found())
    sender = _sender(client, FakeServer("s1"))

    assert await sender.entity_exists("channel", "c1") is False


async def test_channel_fetch_failing_otherwise_cant_tell():
    client = FakeClient()
    sender = _sender(client, FakeServer("s1"))

    client.fetch_channel = _raising(stoat_forbidden())
    assert await sender.entity_exists("channel", "c1") is None
    client.fetch_channel = _raising(LookupError("network"))
    assert await sender.entity_exists("channel", "c1") is None


async def test_user_exists_or_is_gone():
    client = FakeClient()
    client.add_user("u1", SimpleNamespace(id="u1", display_name="someone"))
    sender = _sender(client, FakeServer("s1"))

    assert await sender.entity_exists("user", "u1") is True
    client.fetch_user = _raising(stoat_not_found())
    assert await sender.entity_exists("user", "u2") is False


async def test_cached_role_exists():
    server = FakeServer("s1")
    server.roles = {"r1": SimpleNamespace(id="r1", name="mods", rank=1)}
    sender = _sender(FakeClient(), server)

    assert await sender.entity_exists("role", "r1") is True


async def test_role_missing_from_a_fresh_fetch_is_gone():
    client = FakeClient()
    server = FakeServer("s1")
    sender = _sender(client, server)
    fresh = FakeServer("s1")
    fresh.roles = {"r2": SimpleNamespace(id="r2", name="other", rank=1)}
    client.set_fetched_server(fresh)

    assert await sender.entity_exists("role", "r1") is False


async def test_role_found_only_by_a_fresh_fetch_exists():
    # The cache can drift (issue #66) - a cache miss alone isn't "gone".
    client = FakeClient()
    sender = _sender(client, FakeServer("s1"))
    fresh = FakeServer("s1")
    fresh.roles = {"r1": SimpleNamespace(id="r1", name="mods", rank=1)}
    client.set_fetched_server(fresh)

    assert await sender.entity_exists("role", "r1") is True


async def test_role_cant_tell_when_the_server_fetch_fails():
    client = FakeClient()
    sender = _sender(client, FakeServer("s1"))
    client.fetch_server = _raising(LookupError("network"))

    assert await sender.entity_exists("role", "r1") is None


async def test_emoji_exists_or_is_gone():
    client = FakeClient()
    server = FakeServer("s1")
    server.add_emoji(FakeEmoji(id="e1", name="blobwave"))
    sender = _sender(client, server)

    assert await sender.entity_exists("emoji", "e1") is True
    client.fetch_emoji = _raising(stoat_not_found())
    assert await sender.entity_exists("emoji", "e2") is False


async def test_unknown_kind_cant_tell():
    sender = _sender(FakeClient(), FakeServer("s1"))

    assert await sender.entity_exists("category", "x") is None
