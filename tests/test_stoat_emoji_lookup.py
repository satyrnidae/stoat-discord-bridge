"""StoatSenderService's emoji-name lookups, which back `/mirror emote`'s
"link to a same-named emote already on the destination instead of creating a
duplicate" path. Regression: `Server.emojis` is a Mapping[id, emoji], so
iterating it yields ids, not emoji objects - the name match never hit and a
duplicate was always created.
"""

from __future__ import annotations

import pytest

from stoat_discord_bridge.services.stoat_service import StoatSenderService
from tests.fakes.fake_stoat import FakeClient, FakeEmoji, FakeServer

pytestmark = pytest.mark.asyncio


def _sender(server: FakeServer) -> StoatSenderService:
    client = FakeClient()
    client.add_server(server)
    sender = object.__new__(StoatSenderService)
    sender.connector_id = "stoat"
    sender._client = client
    sender.server_id = server.id
    return sender


async def test_resolve_emoji_id_by_name_matches_from_the_emojis_mapping():
    server = FakeServer("s1")
    server.add_emoji(FakeEmoji(id="01ABC", name="excatsperated"))
    sender = _sender(server)

    assert await sender.resolve_emoji_id_by_name("excatsperated") == "01ABC"
    assert await sender.resolve_emoji_id_by_name("EXCATSPERATED") == "01ABC"
    assert await sender.resolve_emoji_id_by_name("01ABC") == "01ABC"
    assert await sender.resolve_emoji_id_by_name("nope") is None


async def test_resolve_emoji_id_by_name_falls_back_to_fetch_when_cache_empty():
    server = FakeServer("s1")
    sender = _sender(server)
    # emoji known only to the REST fetch, not the cache mapping
    server._emojis.clear()
    real_fetch = server.fetch_emojis

    async def fetch(**kwargs):
        await real_fetch(**kwargs)
        return [FakeEmoji(id="01XYZ", name="blobwave")]

    server.fetch_emojis = fetch

    assert await sender.resolve_emoji_id_by_name("blobwave") == "01XYZ"


async def test_get_emoji_name_reads_the_mapping():
    server = FakeServer("s1")
    server.add_emoji(FakeEmoji(id="01ABC", name="excatsperated"))
    sender = _sender(server)

    assert await sender.get_emoji_name("01ABC") == "excatsperated"
