"""StoatSenderService's cache-only channel lookups - `get_channel_name`,
`get_channel_category_name`, `channels_in_category`, and the
`get/set_channel_role_permission` perm-mirror hooks - all of which reach for
`Client.get_channel(..., partial=False)`.

Spike (issue #8): `Client.get_channel(partial=False)` is a cache-only lookup
(verified against stoat.py 1.2.1, `Client.get_channel` / `MapCache.get_channel`)
that returns the fully-populated cached channel object - `.name` /
`.category` / `.role_permissions` all present - or `None` on a cache miss. It
never raises for a missing channel and never does I/O. Every call site above
therefore just needs a `None` guard, which these tests pin.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from stoat_discord_bridge.services.role_sync import RolePermissionOverride
from stoat_discord_bridge.services.stoat_service import StoatSenderService
from tests.fakes.fake_stoat import FakeCategory, FakeChannel, FakeClient, FakeServer

pytestmark = pytest.mark.asyncio


def _sender(*, client: FakeClient | None = None, server_id: str = "s1") -> StoatSenderService:
    sender = object.__new__(StoatSenderService)
    sender.connector_id = "stoat"
    sender._client = client if client is not None else FakeClient()
    sender.server_id = server_id
    return sender


# --------------------------------------------------------------- get_channel_name


async def test_get_channel_name_from_cache():
    client = FakeClient()
    client.add_channel(FakeChannel(id="c1", name="general"))

    assert await _sender(client=client).get_channel_name("c1") == "general"


async def test_get_channel_name_is_none_on_a_cache_miss():
    assert await _sender().get_channel_name("nope") is None


# ------------------------------------------------------ get_channel_category_name


async def test_get_channel_category_name_from_cache():
    client = FakeClient()
    client.add_channel(FakeChannel(id="c1", category=FakeCategory(id="cat-1", title="Team Alpha")))

    assert await _sender(client=client).get_channel_category_name("c1") == "Team Alpha"


async def test_get_channel_category_name_is_none_when_the_channel_has_no_category():
    client = FakeClient()
    client.add_channel(FakeChannel(id="c1", category=None))

    assert await _sender(client=client).get_channel_category_name("c1") is None


async def test_get_channel_category_name_is_none_on_a_cache_miss():
    assert await _sender().get_channel_category_name("nope") is None


# -------------------------------------------------------------- channels_in_category


async def test_channels_in_category_resolves_names_and_falls_back_to_id_on_a_miss():
    client = FakeClient()
    server = FakeServer("s1")
    server.categories = [FakeCategory(id="cat-1", title="Team", channels=["c1", "c2"])]
    client.add_server(server)
    client.add_channel(FakeChannel(id="c1", name="general"))
    # c2 is listed in the category but not in channel cache -> id fallback

    assert await _sender(client=client).channels_in_category("cat-1") == [("c1", "general"), ("c2", "c2")]


async def test_channels_in_category_is_empty_for_an_unknown_category():
    client = FakeClient()
    client.add_server(FakeServer("s1"))

    assert await _sender(client=client).channels_in_category("cat-1") == []


# ----------------------------------------------- get/set_channel_role_permission


async def test_get_channel_role_permission_is_the_empty_override_on_a_cache_miss():
    result = await _sender().get_channel_role_permission("nope", "role-1")

    assert result == RolePermissionOverride(allow=frozenset(), deny=frozenset())


async def test_set_channel_role_permission_is_a_noop_on_a_cache_miss():
    override = RolePermissionOverride(allow=frozenset({"view_channel"}), deny=frozenset())

    # no channel in cache -> get_channel returns None -> silently skipped
    await _sender().set_channel_role_permission("nope", "role-1", override)


# --------------------------------------------------------------- can_view_channel


def _sender_with_self(client: FakeClient, *, self_id: str = "bot", server_id: str = "s1") -> StoatSenderService:
    sender = _sender(client=client, server_id=server_id)
    sender._self_id = self_id
    return sender


async def test_can_view_channel_true_when_the_bot_member_has_view_permission():
    client = FakeClient()
    server = client.add_server(FakeServer("s1"))
    server.add_member("bot", SimpleNamespace(id="bot"))
    client.add_channel(FakeChannel(id="c1", server_id="s1", viewable_by={"bot"}))

    assert await _sender_with_self(client).can_view_channel("c1") is True


async def test_can_view_channel_false_when_the_bot_member_is_denied():
    client = FakeClient()
    server = client.add_server(FakeServer("s1"))
    server.add_member("bot", SimpleNamespace(id="bot"))
    client.add_channel(FakeChannel(id="c1", server_id="s1", viewable_by=set()))

    assert await _sender_with_self(client).can_view_channel("c1") is False


async def test_can_view_channel_is_none_on_a_cache_miss():
    client = FakeClient()
    client.add_server(FakeServer("s1"))

    assert await _sender_with_self(client).can_view_channel("nope") is None


async def test_can_view_channel_is_none_before_the_bot_id_is_known():
    client = FakeClient()
    client.add_channel(FakeChannel(id="c1", server_id="s1", viewable_by={"bot"}))

    assert await _sender(client=client).can_view_channel("c1") is None
