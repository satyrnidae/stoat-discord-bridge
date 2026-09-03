"""StoatSenderService's `list_*` hooks - the cache-only enumerations that
back Discord's `external_id` slash-command autocomplete (a Discord operator
running `/link role … <service=stoat> …` gets a menu of that Stoat server's
real roles). Same no-I/O `get_server(partial=True)` / `_all_*` paths the
bare-name resolvers use; an uncached server yields an empty list.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from stoat_discord_bridge.services.stoat_service import StoatSenderService
from tests.fakes.fake_stoat import FakeAuthor, FakeCategory, FakeClient, FakeEmoji, FakeServer

pytestmark = pytest.mark.asyncio


def _sender(server: FakeServer | None = None) -> StoatSenderService:
    client = FakeClient()
    if server is not None:
        client.add_server(server)
    sender = object.__new__(StoatSenderService)
    sender.connector_id = "stoat"
    sender._client = client
    sender.server_id = server.id if server is not None else "s1"
    return sender


async def test_list_channels_yields_id_name_pairs():
    server = FakeServer("s1")
    server.channels = [SimpleNamespace(id="c1", name="general"), SimpleNamespace(id="c2", name="off-topic")]

    assert await _sender(server).list_channels() == [("c1", "general"), ("c2", "off-topic")]


async def test_list_categories_uses_the_category_title():
    server = FakeServer("s1")
    server.categories = [FakeCategory(id="cat-1", title="Team"), FakeCategory(id="cat-2", title="Ops")]

    assert await _sender(server).list_categories() == [("cat-1", "Team"), ("cat-2", "Ops")]


async def test_list_roles_reads_the_server_roles():
    server = FakeServer("s1")
    server.roles = [SimpleNamespace(id="r1", name="Admins"), SimpleNamespace(id="r2", name="Mods")]

    assert await _sender(server).list_roles() == [("r1", "Admins"), ("r2", "Mods")]


async def test_list_users_prefers_nick_then_display_name_then_username():
    server = FakeServer("s1")
    server.add_member("u1", FakeAuthor("u1", name="corvid", display_name="Corvid Jay", nick="CJ"))
    server.add_member("u2", FakeAuthor("u2", name="wren"))

    assert await _sender(server).list_users() == [("u1", "CJ"), ("u2", "wren")]


async def test_list_emotes_reads_the_server_emoji_mapping():
    server = FakeServer("s1")
    server.add_emoji(FakeEmoji(id="e1", name="blobwave"))

    assert await _sender(server).list_emotes() == [("e1", "blobwave")]


async def test_list_hooks_are_empty_when_the_server_is_uncached():
    sender = _sender(None)

    assert await sender.list_channels() == []
    assert await sender.list_categories() == []
    assert await sender.list_roles() == []
    assert await sender.list_users() == []
    assert await sender.list_emotes() == []
