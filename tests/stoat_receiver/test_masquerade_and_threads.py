from __future__ import annotations

from stoat_discord_bridge.storage.user_mappings import UserMapping, UserMappingRepository
from tests.fakes.fake_stoat import FakeAsset, FakeAuthor, FakeCategory, FakeChannel, FakeClient, FakeServer
from tests.stoat_receiver.conftest import _make_receiver, _message


# ---------------------------------------------------- linked-user masquerade


async def test_receive_masquerades_as_the_linked_local_user_when_linked(fake_db):
    user_mappings = UserMappingRepository(fake_db)
    await user_mappings.upsert(
        UserMapping(link_group="g1", connector_id="discord", user_id="discord-alice", display_name="discord-alice")
    )
    await user_mappings.upsert(UserMapping(link_group="g1", connector_id="stoat", user_id="u1", display_name="u1"))
    client = FakeClient()
    client.add_user("u1", FakeAuthor(id="u1", display_name="Local Alice", avatar=FakeAsset("https://cdn.example/local.png")))
    channel = client.add_channel(FakeChannel(id="42"))
    receiver = _make_receiver(client, user_mappings)

    await receiver.receive(_message(), target_channel_id="42")

    masquerade = channel.sent[0]["masquerade"]
    assert masquerade.name == "Local Alice"
    assert masquerade.avatar == "https://cdn.example/local.png"


async def test_receive_prefers_the_members_nick_over_the_account_display_name(fake_db):
    # stoat.py's Member.display_name passes straight through to the
    # underlying User's account-level display_name and ignores the member's
    # own per-server nick entirely (confirmed against the installed
    # package) - get_masquerade_identity must check nick itself rather than
    # trusting display_name to already account for it.
    user_mappings = UserMappingRepository(fake_db)
    await user_mappings.upsert(
        UserMapping(link_group="g1", connector_id="discord", user_id="discord-alice", display_name="discord-alice")
    )
    await user_mappings.upsert(UserMapping(link_group="g1", connector_id="stoat", user_id="u1", display_name="u1"))
    client = FakeClient()
    client.add_user(
        "u1",
        FakeAuthor(
            id="u1",
            nick="Server Nickname",
            display_name="Global Alice",
            avatar=FakeAsset("https://cdn.example/nick.png"),
        ),
    )
    channel = client.add_channel(FakeChannel(id="42"))
    receiver = _make_receiver(client, user_mappings)

    await receiver.receive(_message(), target_channel_id="42")

    masquerade = channel.sent[0]["masquerade"]
    assert masquerade.name == "Server Nickname"


async def test_receive_strips_the_discriminator_when_falling_back_to_the_bare_username(fake_db):
    user_mappings = UserMappingRepository(fake_db)
    await user_mappings.upsert(
        UserMapping(link_group="g1", connector_id="discord", user_id="discord-alice", display_name="discord-alice")
    )
    await user_mappings.upsert(UserMapping(link_group="g1", connector_id="stoat", user_id="u1", display_name="u1"))
    client = FakeClient()
    client.add_user("u1", FakeAuthor(id="u1", name="alice", tag="alice#0000", display_name=None))
    channel = client.add_channel(FakeChannel(id="42"))
    receiver = _make_receiver(client, user_mappings)

    await receiver.receive(_message(), target_channel_id="42")

    assert channel.sent[0]["masquerade"].name == "alice"


async def test_receive_uses_the_remote_identity_when_the_sender_isnt_linked(fake_db):
    user_mappings = UserMappingRepository(fake_db)
    client = FakeClient()
    channel = client.add_channel(FakeChannel(id="42"))
    receiver = _make_receiver(client, user_mappings)

    await receiver.receive(_message(), target_channel_id="42")

    masquerade = channel.sent[0]["masquerade"]
    assert masquerade.name == "Alice"
    assert masquerade.avatar == "https://cdn.example/alice.png"


async def test_receive_falls_back_to_fetch_user_when_the_resolved_member_has_no_usable_name(fake_db):
    # stoat.py's Member.name/display_name silently return ""/None (rather
    # than raising or returning the real username) when the Member's
    # internal_user reference isn't a locally cached full User object - a
    # gap in that resolution, not evidence the user has no name. This must
    # not be treated as an unresolvable local user: it should fall through
    # to fetching the User object directly, which always has a real name.
    user_mappings = UserMappingRepository(fake_db)
    await user_mappings.upsert(
        UserMapping(link_group="g1", connector_id="discord", user_id="discord-alice", display_name="discord-alice")
    )
    await user_mappings.upsert(UserMapping(link_group="g1", connector_id="stoat", user_id="u1", display_name="u1"))
    client = FakeClient()
    server = client.add_server(FakeServer(id="srv-1"))
    server.add_member("u1", FakeAuthor(id="u1", name="", tag="", display_name=None, nick=None))
    client.add_user(
        "u1", FakeAuthor(id="u1", name="alice", tag="alice#0000", display_name="Local Alice", avatar=FakeAsset("https://cdn.example/local.png"))
    )
    channel = client.add_channel(FakeChannel(id="42"))
    receiver = _make_receiver(client, user_mappings)

    await receiver.receive(_message(), target_channel_id="42")

    masquerade = channel.sent[0]["masquerade"]
    assert masquerade.name == "Local Alice"
    assert masquerade.avatar == "https://cdn.example/local.png"


async def test_receive_falls_back_to_the_remote_identity_when_the_linked_stoat_user_cant_be_resolved(fake_db):
    user_mappings = UserMappingRepository(fake_db)
    await user_mappings.upsert(
        UserMapping(link_group="g1", connector_id="discord", user_id="discord-alice", display_name="discord-alice")
    )
    await user_mappings.upsert(UserMapping(link_group="g1", connector_id="stoat", user_id="u1", display_name="u1"))
    client = FakeClient()  # stoat user u1 never added - fetch_user raises
    channel = client.add_channel(FakeChannel(id="42"))
    receiver = _make_receiver(client, user_mappings)

    await receiver.receive(_message(), target_channel_id="42")

    masquerade = channel.sent[0]["masquerade"]
    assert masquerade.name == "Alice"
    assert masquerade.avatar == "https://cdn.example/alice.png"


# ------------------------------------------- group_parent_channel_with_threads


class _ThreadCats:
    def __init__(self, ids: set[str], parents: dict[str, str] | None = None) -> None:
        self._ids = set(ids)
        self._parents = dict(parents or {})  # category_id -> parent_channel_id

    async def is_thread_category(self, connector_id: str, category_id: str) -> bool:
        return category_id in self._ids

    async def thread_category_parent(self, connector_id: str, category_id: str) -> str | None:
        return self._parents.get(category_id)


def _thread_grouping_server() -> FakeServer:
    server = FakeServer(id="srv-1")
    server.channels = [
        FakeChannel(id="parent-1", name="bot-config"),
        FakeChannel(id="thread-1", name="cool thread"),
    ]
    server.categories = [
        FakeCategory(id="cat-admin", title="Admin", channels=["parent-1"]),
        FakeCategory(id="cat-bc", title="bot-config", channels=["thread-1"]),
    ]
    return server


async def test_receive_groups_the_parent_channel_atop_the_thread_category():
    client = FakeClient()
    server = client.add_server(_thread_grouping_server())
    client.add_channel(FakeChannel(id="thread-1"))
    receiver = _make_receiver(client)
    receiver._sender._category_linker = _ThreadCats({"cat-bc"})

    await receiver.receive(_message(), target_channel_id="thread-1")

    [payload] = server.server_edits
    cats = {c["title"]: c["channels"] for c in payload["categories"]}
    assert cats["Admin"] == []  # parent pulled out of its old category
    assert cats["bot-config"] == ["parent-1", "thread-1"]  # parent first, then the thread


async def test_receive_groups_the_parent_by_binding_after_a_rename():
    client = FakeClient()
    server = _thread_grouping_server()
    # User renamed both the parent channel and the thread Category on Stoat -
    # name match would fail, but the (parent-1 -> cat-bc) binding still resolves.
    server.channels[0].name = "renamed-parent"
    server.categories[1].title = "Renamed Category"
    client.add_server(server)
    client.add_channel(FakeChannel(id="thread-1"))
    receiver = _make_receiver(client)
    receiver._sender._category_linker = _ThreadCats({"cat-bc"}, {"cat-bc": "parent-1"})

    await receiver.receive(_message(), target_channel_id="thread-1")

    [payload] = server.server_edits
    cats = {c["id"]: c["channels"] for c in payload["categories"]}
    assert cats["cat-admin"] == []
    assert cats["cat-bc"] == ["parent-1", "thread-1"]


async def test_receive_doesnt_group_when_the_option_is_off():
    client = FakeClient()
    server = client.add_server(_thread_grouping_server())
    client.add_channel(FakeChannel(id="thread-1"))
    receiver = _make_receiver(client)
    receiver._sender._category_linker = _ThreadCats({"cat-bc"})
    receiver._sender._config.group_parent_channel_with_threads = False

    await receiver.receive(_message(), target_channel_id="thread-1")

    assert server.server_edits == []


async def test_receive_doesnt_group_for_a_non_thread_category():
    client = FakeClient()
    server = client.add_server(_thread_grouping_server())
    client.add_channel(FakeChannel(id="thread-1"))
    receiver = _make_receiver(client)
    receiver._sender._category_linker = _ThreadCats(set())  # cat-bc not marked a thread category

    await receiver.receive(_message(), target_channel_id="thread-1")

    assert server.server_edits == []


async def test_receive_skips_grouping_when_the_parent_is_already_on_top():
    client = FakeClient()
    server = _thread_grouping_server()
    server.categories[1].channels = ["parent-1", "thread-1"]
    client.add_server(server)
    client.add_channel(FakeChannel(id="thread-1"))
    receiver = _make_receiver(client)
    receiver._sender._category_linker = _ThreadCats({"cat-bc"})

    await receiver.receive(_message(), target_channel_id="thread-1")

    assert server.server_edits == []


