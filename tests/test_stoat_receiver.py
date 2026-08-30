"""Tests for StoatReceiverService against the fake_stoat scaffolding
(tests/fakes/fake_stoat.py) - receive()'s masquerade posting/chunking/
partial-failure behavior, reaction add/remove, and custom emoji mirroring
(aiohttp's image download is monkeypatched, not real).
"""

from __future__ import annotations

from types import SimpleNamespace

import aiohttp
import pytest

from stoat_discord_bridge.models import CustomEmoji, StandardMessage
from stoat_discord_bridge.services.base import PartialRelayError
from stoat_discord_bridge.services.stoat_service import StoatReceiverService, StoatSenderService
from stoat_discord_bridge.storage.user_mappings import UserMapping, UserMappingRepository
from tests.fakes.fake_stoat import FakeAsset, FakeAuthor, FakeCategory, FakeChannel, FakeClient, FakeServer


class _FakeSender:
    """StoatReceiverService only ever reads .connector_id and reuses the
    sender's already-connected client - stand in for StoatSenderService
    without building a real one (whose __init__ makes a real network call,
    see test_stoat_resolve_avatar.py's docstring). get_masquerade_identity is
    the real StoatSenderService implementation, bound onto this fake, since
    it's plain client-reading logic with no network-touching __init__ of its
    own to avoid."""

    def __init__(
        self,
        client: FakeClient,
        connector_id: str = "stoat",
        server_id: str = "srv-1",
        enable_local_user_masquerade: bool = True,
    ) -> None:
        self.connector_id = connector_id
        self.server_id = server_id
        self.self_id = "bridge-bot-id"
        self._client = client
        self._category_linker = None
        self._config = SimpleNamespace(
            enable_local_user_masquerade=enable_local_user_masquerade,
            group_parent_channel_with_threads=True,
        )

    def get_channel(self, channel_id: str, *, partial: bool = True):
        return self._client.get_channel(channel_id, partial=partial)

    def get_server(self, server_id: str, *, partial: bool = True):
        return self._client.get_server(server_id, partial=partial)

    get_masquerade_identity = StoatSenderService.get_masquerade_identity
    group_parent_channel_with_threads = StoatSenderService.group_parent_channel_with_threads
    _move_channel_to_category_top = StoatSenderService._move_channel_to_category_top


def _message(**overrides) -> StandardMessage:
    defaults = dict(
        origin_connector_id="discord",
        origin_channel_id="d-100",
        channel_name="general",
        sender_name="Alice",
        sender_avatar_url="https://cdn.example/alice.png",
        sender_user_id="discord-alice",
        content_markdown="hello",
        message_id="m1",
    )
    defaults.update(overrides)
    return StandardMessage(**defaults)


def _make_receiver(client: FakeClient, user_mappings: UserMappingRepository | None = None) -> StoatReceiverService:
    return StoatReceiverService(_FakeSender(client), user_mappings=user_mappings)


# ---------------------------------------------------------------- receive()


async def test_receive_posts_through_masquerade():
    client = FakeClient()
    channel = client.add_channel(FakeChannel(id="42"))
    receiver = _make_receiver(client)

    ids = await receiver.receive(_message(), target_channel_id="42")

    assert channel.sent == [{"content": "hello", "masquerade": channel.sent[0]["masquerade"]}]
    masquerade = channel.sent[0]["masquerade"]
    assert masquerade.name == "Alice"
    assert masquerade.avatar == "https://cdn.example/alice.png"
    assert ids == ["1"]


async def test_receive_truncates_a_sender_name_over_32_chars():
    client = FakeClient()
    channel = client.add_channel(FakeChannel(id="42"))
    receiver = _make_receiver(client)

    await receiver.receive(_message(sender_name="x" * 40), target_channel_id="42")

    assert channel.sent[0]["masquerade"].name == "x" * 32


async def test_receive_splits_long_content_into_multiple_sends(monkeypatch):
    client = FakeClient()
    channel = client.add_channel(FakeChannel(id="42"))
    receiver = _make_receiver(client)
    monkeypatch.setattr("stoat_discord_bridge.services.stoat_service._CONTENT_LIMIT", 5)

    ids = await receiver.receive(_message(content_markdown="abcdefghij"), target_channel_id="42")

    assert [call["content"] for call in channel.sent] == ["abcde", "fghij"]
    assert ids == ["1", "2"]


async def test_receive_raises_partial_relay_error_and_keeps_ids_already_sent(monkeypatch):
    client = FakeClient()
    channel = client.add_channel(FakeChannel(id="42"))
    receiver = _make_receiver(client)
    monkeypatch.setattr("stoat_discord_bridge.services.stoat_service._CONTENT_LIMIT", 5)

    # first chunk should succeed before the second fails.
    real_send = channel.send
    call_count = 0

    async def flaky_send(content, *, masquerade=None):
        nonlocal call_count
        call_count += 1
        if call_count == 2:
            raise RuntimeError("rate limited")
        return await real_send(content, masquerade=masquerade)

    channel.send = flaky_send

    with pytest.raises(PartialRelayError) as exc_info:
        await receiver.receive(_message(content_markdown="abcdefghij"), target_channel_id="42")

    assert exc_info.value.partial_ids == ["1"]


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


# ---------------------------------------------------------------- reactions


async def test_add_reaction_targets_the_right_message():
    client = FakeClient()
    channel = client.add_channel(FakeChannel(id="42"))
    receiver = _make_receiver(client)

    await receiver.add_reaction(target_channel_id="42", target_message_id="7", emoji="\U0001f600")

    assert channel.get_message("7").added_reactions == ["\U0001f600"]


async def test_remove_reaction_targets_the_right_message():
    client = FakeClient()
    channel = client.add_channel(FakeChannel(id="42"))
    channel.get_message("7").reactions["\U0001f600"] = ("bridge-bot-id",)
    receiver = _make_receiver(client)

    await receiver.remove_reaction(target_channel_id="42", target_message_id="7", emoji="\U0001f600")

    assert channel.get_message("7").removed_reactions == ["\U0001f600"]


async def test_add_reaction_skips_when_the_bot_already_reacted():
    client = FakeClient()
    channel = client.add_channel(FakeChannel(id="42"))
    channel.get_message("7").reactions["\U0001f600"] = ("someone", "bridge-bot-id")
    receiver = _make_receiver(client)

    await receiver.add_reaction(target_channel_id="42", target_message_id="7", emoji="\U0001f600")

    assert channel.get_message("7").added_reactions == []


async def test_remove_reaction_skips_when_the_bot_isnt_reacting():
    client = FakeClient()
    channel = client.add_channel(FakeChannel(id="42"))
    channel.get_message("7").reactions["\U0001f600"] = ("someone-else",)
    receiver = _make_receiver(client)

    await receiver.remove_reaction(target_channel_id="42", target_message_id="7", emoji="\U0001f600")

    assert channel.get_message("7").removed_reactions == []


async def test_add_reaction_translates_a_custom_emoji_to_its_native_id():
    client = FakeClient()
    channel = client.add_channel(FakeChannel(id="42"))
    receiver = _make_receiver(client)

    await receiver.add_reaction(
        target_channel_id="42",
        target_message_id="7",
        emoji=CustomEmoji(native_id="stoat-555", name="smile", image_url="https://cdn.example/e.png"),
    )

    assert channel.get_message("7").added_reactions == ["stoat-555"]


# ---------------------------------------------------------------- create_emoji


class _FakeAiohttpResponse:
    def __init__(self, body: bytes, *, status: int = 200) -> None:
        self._body = body
        self.status = status

    async def __aenter__(self) -> "_FakeAiohttpResponse":
        return self

    async def __aexit__(self, *exc_info) -> bool:
        return False

    def raise_for_status(self) -> None:
        if self.status >= 400:
            raise aiohttp.ClientResponseError(None, (), status=self.status)

    async def read(self) -> bytes:
        return self._body


async def test_create_emoji_downloads_and_mirrors_it(monkeypatch):
    monkeypatch.setattr(aiohttp.ClientSession, "get", lambda self, url: _FakeAiohttpResponse(b"image-bytes"))
    client = FakeClient()
    server = client.add_server(FakeServer(id="srv-1"))
    receiver = _make_receiver(client)

    result = await receiver.create_emoji(CustomEmoji(native_id="e1", name="smile", image_url="https://cdn.example/e.png"))

    assert server.created_emoji_calls == [{"name": "smile", "image": b"image-bytes"}]
    assert result is not None
    assert result.name == "smile"


async def test_create_emoji_returns_none_on_http_failure(monkeypatch):
    monkeypatch.setattr(aiohttp.ClientSession, "get", lambda self, url: _FakeAiohttpResponse(b"image-bytes"))
    client = FakeClient()
    client.add_server(
        FakeServer(id="srv-1", raises=aiohttp.ClientResponseError(SimpleNamespace(real_url="https://cdn.example"), (), status=400))
    )
    receiver = _make_receiver(client)

    result = await receiver.create_emoji(CustomEmoji(native_id="e1", name="smile", image_url="https://cdn.example/e.png"))

    assert result is None


# ---------------------------------------------------------------- set_pinned


async def test_set_pinned_pins_and_unpins_the_message():
    client = FakeClient()
    channel = client.add_channel(FakeChannel(id="c-1"))
    receiver = _make_receiver(client)

    await receiver.set_pinned(target_channel_id="c-1", target_message_id="m7", pinned=True)
    msg = await channel.fetch_message("m7")
    assert msg.pinned is True and msg.pin_calls == 1

    await receiver.set_pinned(target_channel_id="c-1", target_message_id="m7", pinned=False)
    assert msg.pinned is False and msg.unpin_calls == 1


async def test_set_pinned_is_a_noop_when_already_in_the_target_state():
    client = FakeClient()
    channel = client.add_channel(FakeChannel(id="c-1"))
    msg = await channel.fetch_message("m7")
    msg.pinned = True
    receiver = _make_receiver(client)

    await receiver.set_pinned(target_channel_id="c-1", target_message_id="m7", pinned=True)

    assert msg.pin_calls == 0


# ---------------------------------------------------------------- trigger_typing


async def test_trigger_typing_keeps_typing_then_ends_it():
    client = FakeClient()
    channel = client.add_channel(FakeChannel(id="c-1"))
    receiver = _make_receiver(client)
    receiver._TYPING_LINGER = 0.05
    receiver._TYPING_REFRESH = 0.01

    await receiver.trigger_typing(target_channel_id="c-1")
    await receiver._typing_tasks["c-1"]

    assert channel.typing_events[0] == "begin"
    assert channel.typing_events[-1] == "end"
    assert receiver._typing_tasks == {}


async def test_trigger_typing_reuses_the_running_loop_for_repeat_calls():
    client = FakeClient()
    client.add_channel(FakeChannel(id="c-1"))
    receiver = _make_receiver(client)
    receiver._TYPING_LINGER = 0.05
    receiver._TYPING_REFRESH = 0.01

    await receiver.trigger_typing(target_channel_id="c-1")
    task = receiver._typing_tasks["c-1"]
    await receiver.trigger_typing(target_channel_id="c-1")

    assert receiver._typing_tasks["c-1"] is task
    await task
