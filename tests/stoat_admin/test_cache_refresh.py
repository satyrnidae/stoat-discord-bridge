from __future__ import annotations

import stoat

from stoat_discord_bridge.models import ChannelMetadata
from tests.fakes.fake_stoat import FakeAsset, FakeCategory, FakeChannel, FakeClient, FakeServer
from tests.stoat_admin.conftest import _make_sender


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


# ---- issue #27: the whole-server category PATCH is rebuilt from a fresh fetch,
# ---- never the (possibly stale) cached server, so it can't revert the layout


async def test_place_via_server_edit_rebuilds_from_a_freshly_fetched_server():
    # The cache still shows the category layout from gateway-connect time; the
    # real server has gained a whole Category since. PATCHing the stale
    # snapshot straight back would delete that Category server-side (issue #27).
    stale = FakeServer(id="s1")
    stale.categories = [FakeCategory(id="cat-a", title="Alpha", channels=["ch-1"])]
    fresh = FakeServer(id="s1")
    fresh.categories = [
        FakeCategory(id="cat-a", title="Alpha", channels=["ch-1"]),
        FakeCategory(id="cat-b", title="Beta", channels=["ch-2"]),
    ]
    client = FakeClient()
    client.add_server(stale)
    client.set_fetched_server(fresh)
    sender = _make_sender(client=client)

    await sender._place_via_server_edit(stale, "ch-new", "Alpha")

    [payload] = fresh.server_edits
    cats = {c["id"]: c["channels"] for c in payload["categories"]}
    assert cats == {"cat-a": ["ch-1", "ch-new"], "cat-b": ["ch-2"]}  # Beta not dropped


async def test_place_via_server_edit_reuses_a_linked_category_absent_from_the_cache():
    # The linked Category was created after startup, so the cache lacks it;
    # matching against the stale list would miss it and mint a fresh-id
    # Category, orphaning the /link-category mapping (issue #27).
    stale = FakeServer(id="s1")  # nothing cached
    fresh = FakeServer(id="s1")
    fresh.categories = [FakeCategory(id="cat-real", title="Team", channels=["ch-1"])]
    client = FakeClient()
    client.add_server(stale)
    client.set_fetched_server(fresh)
    sender = _make_sender(client=client)

    resolved = await sender._place_via_server_edit(stale, "ch-new", "Team")

    assert resolved.id == "cat-real"  # reused, not recreated under a new id
    [payload] = fresh.server_edits
    assert [c["id"] for c in payload["categories"]] == ["cat-real"]
    assert payload["categories"][0]["channels"] == ["ch-1", "ch-new"]


async def test_place_via_server_edit_preserves_untouched_category_permissions():
    class RichCategory:
        def __init__(self, id, title, channels, extra):
            self.id, self.title, self.channels, self._extra = id, title, channels, extra

        def to_dict(self):
            return {"id": self.id, "title": self.title, "channels": list(self.channels), **self._extra}

    fresh = FakeServer(id="s1")
    fresh.categories = [
        RichCategory("cat-a", "Alpha", ["ch-1"], {"role_permissions": {"r1": {"a": 4, "d": 0}}}),
    ]
    client = FakeClient()
    client.add_server(FakeServer(id="s1"))
    client.set_fetched_server(fresh)
    sender = _make_sender(client=client)

    await sender._place_via_server_edit(fresh, "ch-new", "Alpha")

    [payload] = fresh.server_edits
    assert payload["categories"][0]["role_permissions"] == {"r1": {"a": 4, "d": 0}}
    assert payload["categories"][0]["channels"] == ["ch-1", "ch-new"]


async def test_move_channel_to_category_top_rebuilds_from_a_freshly_fetched_server():
    stale = FakeServer(id="s1")
    stale.categories = [FakeCategory(id="cat-x", title="X", channels=["p1"])]
    fresh = FakeServer(id="s1")
    fresh.categories = [
        FakeCategory(id="cat-x", title="X", channels=["p1"]),
        FakeCategory(id="cat-y", title="Y", channels=["t1"]),
    ]
    client = FakeClient()
    client.add_server(stale)
    client.set_fetched_server(fresh)
    sender = _make_sender(client=client)

    await sender._move_channel_to_category_top(stale, "p1", "cat-y")

    [payload] = fresh.server_edits
    cats = {c["id"]: c["channels"] for c in payload["categories"]}
    assert cats == {"cat-x": [], "cat-y": ["p1", "t1"]}  # cat-y (post-startup) not lost


async def test_ensure_category_server_edit_fallback_builds_from_a_fresh_fetch():
    class OldStoatServer(FakeServer):
        async def create_category(self, name, *, channels):
            raise stoat.HTTPException.__new__(stoat.NotFound)

    fresh = OldStoatServer(id="s1")
    fresh.categories = [FakeCategory(id="cat-b", title="Beta", channels=["ch-2"])]
    client = FakeClient()
    client.add_server(OldStoatServer(id="s1"))  # stale cache: no categories
    client.set_fetched_server(fresh)
    sender = _make_sender(client=client)

    new_id = await sender.ensure_category("Gamma")

    [payload] = fresh.server_edits
    by_title = {c["title"]: c["id"] for c in payload["categories"]}
    assert by_title["Beta"] == "cat-b"  # the post-startup Category isn't dropped
    assert by_title["Gamma"] == new_id


async def test_ensure_category_reuses_an_existing_match_from_the_fresh_fetch():
    fresh = FakeServer(id="s1")
    fresh.categories = [FakeCategory(id="cat-real", title="Team", channels=["ch-1"])]
    client = FakeClient()
    client.add_server(FakeServer(id="s1"))  # stale cache doesn't know "Team"
    client.set_fetched_server(fresh)
    sender = _make_sender(client=client)

    assert await sender.ensure_category("team") == "cat-real"  # case-insensitive reuse
    assert fresh.server_edits == []  # nothing recreated


async def test_move_channel_to_category_builds_from_a_fresh_fetch():
    fresh = FakeServer(id="s1")
    fresh.categories = [
        FakeCategory(id="cat-x", title="X", channels=["a"]),
        FakeCategory(id="cat-y", title="Y", channels=[]),
    ]
    client = FakeClient()
    client.add_server(FakeServer(id="s1"))  # stale cache only knows cat-x
    client.set_fetched_server(fresh)
    sender = _make_sender(client=client)

    await sender.move_channel_to_category("a", "cat-y")

    [payload] = fresh.server_edits
    cats = {c["id"]: c["channels"] for c in payload["categories"]}
    assert cats == {"cat-x": [], "cat-y": ["a"]}


# ---- issue #66: the Category-list *readers* also re-fetch (short-TTL-cached),
# ---- since stoat.py never updates the cached `.categories` from gateway events,
# ---- so `/link category` for a Category created since startup would otherwise
# ---- fail to resolve the name and store the raw token as the "id".


class _CountingClient(FakeClient):
    def __init__(self) -> None:
        super().__init__()
        self.fetch_server_calls = 0

    async def fetch_server(self, server_id: str, *, populate_channels: bool = False):
        self.fetch_server_calls += 1
        return await super().fetch_server(server_id, populate_channels=populate_channels)


def _drifted_client() -> _CountingClient:
    """A cache that only knew "Alpha" at gateway-connect; the live server has
    gained "Counting" since."""
    stale = FakeServer(id="s1")
    stale.categories = [FakeCategory(id="cat-a", title="Alpha", channels=[])]
    fresh = FakeServer(id="s1")
    fresh.categories = [
        FakeCategory(id="cat-a", title="Alpha", channels=[]),
        FakeCategory(id="cat-count", title="Counting", channels=["ch-1"]),
    ]
    client = _CountingClient()
    client.add_server(stale)
    client.set_fetched_server(fresh)
    return client


async def test_resolve_category_id_by_name_finds_a_category_created_since_startup():
    sender = _make_sender(client=_drifted_client())

    assert await sender.resolve_category_id_by_name("counting") == "cat-count"
    assert await sender.resolve_category_id_by_name("cat-count") == "cat-count"
    assert await sender.resolve_category_id_by_name("nope") is None


async def test_get_category_name_finds_a_category_created_since_startup():
    sender = _make_sender(client=_drifted_client())

    assert await sender.get_category_name("cat-count") == "Counting"


async def test_list_categories_includes_a_category_created_since_startup():
    sender = _make_sender(client=_drifted_client())

    assert await sender.list_categories() == [("cat-a", "Alpha"), ("cat-count", "Counting")]


async def test_fresh_category_reads_are_ttl_cached():
    client = _drifted_client()
    sender = _make_sender(client=client)

    await sender.list_categories()
    await sender.resolve_category_id_by_name("counting")
    await sender.get_category_name("cat-count")

    assert client.fetch_server_calls == 1  # later reads served from the TTL cache


async def test_a_category_write_invalidates_the_read_cache():
    client = _drifted_client()
    sender = _make_sender(client=client)

    await sender.list_categories()
    fetches_before_write = client.fetch_server_calls

    await sender.ensure_category("Brand New")  # mutates the layout
    await sender.list_categories()

    # the post-write read re-fetched rather than serving the pre-write snapshot
    assert client.fetch_server_calls > fetches_before_write + 1


