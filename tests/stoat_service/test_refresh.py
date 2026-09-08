from __future__ import annotations

from tests.fakes.fake_stoat import FakeCategory, FakeClient, FakeServer
from tests.stoat_service.conftest import _make_sender


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


