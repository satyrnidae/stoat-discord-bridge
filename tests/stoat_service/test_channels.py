from __future__ import annotations

import stoat

from stoat_discord_bridge.models import ChannelMetadata
from tests.fakes.fake_stoat import FakeAsset, FakeCategory, FakeChannel, FakeClient, FakeServer
from tests.stoat_service.conftest import _make_sender


# ---------------------------------------------------------------- ensure_channel / Category placement


async def test_ensure_channel_creates_a_new_category_when_none_matches():
    server = FakeServer(id="s1")
    client = FakeClient()
    client.add_server(server)
    sender = _make_sender(client=client)

    channel_id = await sender.ensure_channel("general", "Team Alpha")

    assert channel_id == "chan-general"
    assert server.created_categories == [{"name": "Team Alpha", "channels": ["chan-general"]}]
    [category] = server.categories
    assert category.title == "Team Alpha"
    assert category.channels == ["chan-general"]


async def test_ensure_channel_adds_to_an_existing_category_by_title():
    server = FakeServer(id="s1")
    server.categories.append(FakeCategory(id="cat-1", title="Team Alpha", channels=["chan-other"]))
    client = FakeClient()
    client.add_server(server)
    sender = _make_sender(client=client)

    channel_id = await sender.ensure_channel("general", "Team Alpha")

    assert channel_id == "chan-general"
    assert server.created_categories == []  # matched the existing one - no new Category created
    [category] = server.categories
    assert category.channels == ["chan-other", "chan-general"]


async def test_ensure_channel_is_idempotent_when_channel_already_in_category():
    server = FakeServer(id="s1")
    server.categories.append(FakeCategory(id="cat-1", title="Team Alpha", channels=["chan-general"]))
    channel = FakeChannel(id="chan-general", name="general")
    server.channels.append(channel)
    client = FakeClient()
    client.add_server(server)
    sender = _make_sender(client=client)

    await sender.ensure_channel("general", "Team Alpha")

    [category] = server.categories
    assert category.channels == ["chan-general"]  # not duplicated


async def test_ensure_channel_without_a_category_leaves_categories_untouched():
    server = FakeServer(id="s1")
    client = FakeClient()
    client.add_server(server)
    sender = _make_sender(client=client)

    channel_id = await sender.ensure_channel("general")

    assert channel_id == "chan-general"
    assert server.categories == []
    assert server.created_categories == []


async def test_ensure_channel_reports_channel_even_if_category_placement_fails():
    class ExplodingServer(FakeServer):
        async def create_category(self, name, *, channels):
            raise RuntimeError("category creation failed")

    server = ExplodingServer(id="s1")
    client = FakeClient()
    client.add_server(server)
    sender = _make_sender(client=client)

    channel_id = await sender.ensure_channel("general", "Team Alpha")

    assert channel_id == "chan-general"  # channel creation itself still succeeded


# ---------------------------------------------------------------- ensure_channel metadata (issue #32)


async def test_ensure_channel_applies_description_and_nsfw_when_it_creates_the_channel():
    server = FakeServer(id="s1")
    client = FakeClient()
    client.add_server(server)
    sender = _make_sender(client=client)

    await sender.ensure_channel(
        "general", metadata=ChannelMetadata(description="the general channel", nsfw=True)
    )

    assert server.created_channel_calls == [
        {"name": "general", "description": "the general channel", "nsfw": True}
    ]


async def test_ensure_channel_downloads_and_sets_the_icon_on_create(monkeypatch):
    async def fake_download(url):
        assert url == "https://cdn.example/icon.png"
        return b"icon-bytes"

    monkeypatch.setattr(
        "stoat_discord_bridge.services.stoat_service.lookups.channels._download", fake_download
    )
    server = FakeServer(id="s1")
    client = FakeClient()
    client.add_server(server)
    sender = _make_sender(client=client)

    await sender.ensure_channel(
        "general", metadata=ChannelMetadata(icon_url="https://cdn.example/icon.png")
    )

    [created] = server.channels
    assert created.edits == [{"icon": created.icon}]  # channel.edit(icon=<Upload>) fired once


async def test_ensure_channel_leaves_an_existing_channels_metadata_alone():
    server = FakeServer(id="s1")
    existing = FakeChannel(id="chan-general", name="general", description="hand-written", nsfw=False)
    server.channels.append(existing)
    client = FakeClient()
    client.add_server(server)
    sender = _make_sender(client=client)

    channel_id = await sender.ensure_channel(
        "general", metadata=ChannelMetadata(description="from the source", nsfw=True)
    )

    assert channel_id == "chan-general"
    assert server.created_channel_calls == []  # nothing created
    assert existing.description == "hand-written"  # and the match wasn't edited
    assert existing.edits == []


async def test_describe_channel_reads_description_nsfw_and_icon():
    server = FakeServer(id="s1")
    channel = FakeChannel(
        id="c1", name="general", description="a channel", nsfw=True, icon=FakeAsset("https://cdn.example/i.png")
    )
    client = FakeClient()
    client.add_channel(channel)
    client.add_server(server)
    sender = _make_sender(client=client)

    meta = await sender.describe_channel("c1")

    assert meta == ChannelMetadata(
        description="a channel", nsfw=True, icon_url="https://cdn.example/i.png"
    )


async def test_describe_channel_returns_none_for_an_unresolvable_channel():
    client = FakeClient()
    client.add_server(FakeServer(id="s1"))
    sender = _make_sender(client=client)

    assert await sender.describe_channel("nope") is None


async def test_ensure_channel_falls_back_to_server_edit_when_the_category_endpoint_404s():
    class OldStoatServer(FakeServer):
        async def create_category(self, name, *, channels):
            raise stoat.HTTPException.__new__(stoat.NotFound)  # older API: POST /servers/{id}/categories 404s

    server = OldStoatServer(id="s1")
    client = FakeClient()
    client.add_server(server)
    sender = _make_sender(client=client)

    channel_id = await sender.ensure_channel("general", "Team Alpha")

    assert channel_id == "chan-general"
    assert server.created_categories == []
    [payload] = server.server_edits
    [category] = payload["categories"]
    assert category["title"] == "Team Alpha"
    assert category["channels"] == ["chan-general"]
    assert category["id"]  # a generated id


async def test_ensure_channel_server_edit_fallback_adds_to_an_existing_category():
    class OldStoatServer(FakeServer):
        async def create_category(self, name, *, channels):
            raise stoat.HTTPException.__new__(stoat.NotFound)

        async def edit_category(self, category, *, channels):
            raise stoat.HTTPException.__new__(stoat.NotFound)

    server = OldStoatServer(id="s1")
    server.categories.append(FakeCategory(id="cat-1", title="Bot Config", channels=["chan-other"]))
    client = FakeClient()
    client.add_server(server)
    sender = _make_sender(client=client)

    await sender.ensure_channel("general", "Bot Config")

    [payload] = server.server_edits
    [category] = payload["categories"]
    assert category["id"] == "cat-1"  # reused, not recreated
    assert category["channels"] == ["chan-other", "chan-general"]


async def test_ensure_channel_retries_category_placement_against_a_refetched_server():
    attempts = []

    class FlakyServer(FakeServer):
        async def create_category(self, name, *, channels):
            attempts.append(name)
            if len(attempts) == 1:
                raise RuntimeError("stale cache: duplicate category")
            return await super().create_category(name, channels=channels)

    server = FlakyServer(id="s1")
    client = FakeClient()
    client.add_server(server)
    sender = _make_sender(client=client)

    channel_id = await sender.ensure_channel("general", "Team Alpha")

    assert channel_id == "chan-general"
    assert attempts == ["Team Alpha", "Team Alpha"]  # failed once, retried after re-fetch
    assert [c.title for c in server.categories] == ["Team Alpha"]


