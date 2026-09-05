from __future__ import annotations

from types import SimpleNamespace

from tests.fakes.fake_stoat import FakeCategory, FakeChannel, FakeClient, FakeServer
from tests.stoat_admin.conftest import FakeLinker, _make_ctx, _make_sender


# --------------------------------- thread-Category binding (parent <-> category id)


class _BindingLinker:
    def __init__(self, bound: dict[str, str] | None = None) -> None:
        self.bound = dict(bound or {})  # parent_channel_id -> category_id
        self.binds: list[tuple[str, str]] = []
        self.forgotten: list[str] = []

    async def thread_category_id(self, connector_id, parent_channel_id):
        return self.bound.get(parent_channel_id)

    async def bind_thread_category(self, connector_id, parent_channel_id, category_id):
        self.bound[parent_channel_id] = category_id
        self.binds.append((parent_channel_id, category_id))

    async def forget_thread_category(self, connector_id, parent_channel_id):
        self.bound.pop(parent_channel_id, None)
        self.forgotten.append(parent_channel_id)

    async def is_thread_category(self, connector_id, category_id):
        return category_id in self.bound.values()


async def test_ensure_channel_binds_parent_to_category_on_first_thread():
    server = FakeServer(id="s1")
    server.categories.append(FakeCategory(id="cat-1", title="Bot Config", channels=[]))
    client = FakeClient()
    client.add_server(server)
    linker = _BindingLinker()
    sender = _make_sender(client=client, category_linker=linker)

    await sender.ensure_channel("general", "Bot Config", True, "p1")

    assert linker.binds == [("p1", "cat-1")]  # matched the existing Category by title, then bound it


async def test_ensure_channel_reuses_the_bound_category_by_id_after_a_rename():
    server = FakeServer(id="s1")
    server.categories.append(FakeCategory(id="cat-1", title="Renamed On Stoat", channels=["chan-other"]))
    client = FakeClient()
    client.add_server(server)
    linker = _BindingLinker({"p1": "cat-1"})
    sender = _make_sender(client=client, category_linker=linker)

    await sender.ensure_channel("general", "Bot Config", True, "p1")

    assert server.created_categories == []  # no new Category despite the title mismatch
    [category] = server.categories
    assert category.channels == ["chan-other", "chan-general"]


async def test_ensure_channel_self_heals_when_the_bound_category_is_gone():
    server = FakeServer(id="s1")  # nothing with id "cat-gone"
    client = FakeClient()
    client.add_server(server)
    linker = _BindingLinker({"p1": "cat-gone"})
    sender = _make_sender(client=client, category_linker=linker)

    await sender.ensure_channel("general", "Bot Config", True, "p1")

    assert linker.forgotten == ["p1"]  # stale binding dropped
    [category] = server.categories
    assert category.title == "Bot Config"  # fresh Category created by the linked parent name
    assert linker.bound["p1"] == category.id  # and rebound to the new id
    assert linker.binds == [("p1", category.id)]


async def test_ensure_channel_keeps_a_bound_thread_category_absent_from_the_stale_cache():
    # The thread Category was created on an earlier thread via the raw-HTTP
    # PATCH, which doesn't refresh the client cache - so `get_server` still
    # doesn't list it. Judging "is it gone?" off the cache would forget the
    # binding and spawn a duplicate Category for every later thread (issue #27,
    # thread path). The bound-category check must run against a fresh fetch.
    stale = FakeServer(id="s1")  # cache never saw cat-thread
    fresh = FakeServer(id="s1")
    fresh.categories = [FakeCategory(id="cat-thread", title="Renamed On Stoat", channels=["chan-other"])]
    client = FakeClient()
    client.add_server(stale)
    client.set_fetched_server(fresh)
    linker = _BindingLinker({"p1": "cat-thread"})
    sender = _make_sender(client=client, category_linker=linker)

    await sender.ensure_channel("general", "Bot Config", True, "p1")

    assert linker.forgotten == []  # binding kept - the Category isn't actually gone
    assert linker.binds == [("p1", "cat-thread")]  # re-affirmed to the same id, not a new one
    assert fresh.created_categories == []  # no duplicate Category
    [category] = fresh.categories
    assert category.channels == ["chan-other", "chan-general"]  # channel added to the bound one


async def test_ensure_channel_dedupes_against_a_freshly_fetched_channel_list():
    # A channel created since gateway-connect (e.g. by another connector's
    # mirror) that the cache doesn't list must not be re-created.
    stale = FakeServer(id="s1")
    fresh = FakeServer(id="s1")
    fresh.channels = [FakeChannel(id="chan-existing", name="general")]
    client = FakeClient()
    client.add_server(stale)
    client.set_fetched_server(fresh)
    sender = _make_sender(client=client)

    channel_id = await sender.ensure_channel("general")

    assert channel_id == "chan-existing"
    assert fresh.created_channels == []


async def test_ensure_channel_groups_the_parent_channel_atop_the_thread_category():
    # `/mirror channel` on a Discord thread must pull the parent channel up into
    # the freshly-created thread Category now, not leave it to the next relayed
    # message (issue #94) - `group_parent_channel_with_threads` on the relay path
    # reads the cache-only Category list, which never carries this brand-new
    # Category.
    server = FakeServer(id="s1")
    server.channels = [FakeChannel(id="p1", name="bot-config"), FakeChannel(id="chan-general", name="general")]
    server.categories = [
        FakeCategory(id="cat-admin", title="Admin", channels=["p1"]),
        FakeCategory(id="cat-1", title="Bot Config", channels=[]),
    ]
    client = FakeClient()
    client.add_server(server)
    linker = _BindingLinker()
    sender = _make_sender(client=client, category_linker=linker)

    await sender.ensure_channel("general", "Bot Config", True, "p1")

    assert linker.binds == [("p1", "cat-1")]
    [payload] = server.server_edits
    cats = {c["title"]: c["channels"] for c in payload["categories"]}
    assert cats["Admin"] == []  # parent pulled out of its old category
    assert cats["Bot Config"] == ["p1", "chan-general"]  # parent first, then the thread channel


async def test_ensure_channel_skips_the_parent_group_when_it_already_leads_the_category():
    server = FakeServer(id="s1")
    server.channels = [FakeChannel(id="p1", name="bot-config"), FakeChannel(id="chan-general", name="general")]
    server.categories = [FakeCategory(id="cat-1", title="Bot Config", channels=["p1"])]
    client = FakeClient()
    client.add_server(server)
    sender = _make_sender(client=client, category_linker=_BindingLinker())

    await sender.ensure_channel("general", "Bot Config", True, "p1")

    assert server.server_edits == []  # parent already on top - nothing rebuilt


async def test_ensure_channel_skips_the_parent_group_when_the_option_is_off():
    server = FakeServer(id="s1")
    server.channels = [FakeChannel(id="p1", name="bot-config"), FakeChannel(id="chan-general", name="general")]
    server.categories = [
        FakeCategory(id="cat-admin", title="Admin", channels=["p1"]),
        FakeCategory(id="cat-1", title="Bot Config", channels=[]),
    ]
    client = FakeClient()
    client.add_server(server)
    sender = _make_sender(client=client, category_linker=_BindingLinker())
    sender._config = SimpleNamespace(group_parent_channel_with_threads=False)

    await sender.ensure_channel("general", "Bot Config", True, "p1")

    assert server.server_edits == []


