from __future__ import annotations

import stoat

from tests.fakes.fake_stoat import FakeCategory, FakeClient, FakeServer
from tests.stoat_service.conftest import _make_sender


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


