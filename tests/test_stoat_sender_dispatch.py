"""Tests for StoatSenderService's gateway-event handlers - _handle_message
(including its /status command short-circuit and avatar resolution),
_handle_reaction, and on_emoji_create/on_emoji_delete - against the
fake_stoat scaffolding.

Constructs the service via object.__new__ rather than StoatSenderService(...)
directly, same as test_stoat_resolve_avatar.py: __init__ builds a
_StoatClient whose constructor makes a real network call
(_discover_websocket_base), which none of these handlers actually need.
"""

from __future__ import annotations

from types import SimpleNamespace

from stoat_discord_bridge.models import CustomEmoji
from stoat_discord_bridge.services.stoat_service import StoatSenderService
from stoat_discord_bridge.status import HealthTracker
from tests.fakes.fake_stoat import FakeAsset, FakeAuthor, FakeChannel, FakeClient, FakeServer


class _Recorder:
    def __init__(self) -> None:
        self.messages: list = []
        self.reactions: list = []
        self.emoji_created: list = []
        self.emoji_deleted: list = []

    async def on_message(self, message) -> None:
        self.messages.append(message)

    async def on_reaction(self, reaction) -> None:
        self.reactions.append(reaction)

    async def on_emoji_created(self, created) -> None:
        self.emoji_created.append(created)

    async def on_emoji_deleted(self, deleted) -> None:
        self.emoji_deleted.append(deleted)


def _make_sender(
    recorder: _Recorder,
    client: FakeClient,
    *,
    self_id: str | None = "bridge-bot-id",
    with_reactions: bool = True,
    with_emoji: bool = True,
) -> StoatSenderService:
    sender = object.__new__(StoatSenderService)
    sender.connector_id = "stoat"
    sender._client = client
    sender._health = HealthTracker({"stoat": "Stoat"})
    sender._linker = None
    sender._self_id = self_id
    sender._on_message = recorder.on_message
    sender._on_reaction = recorder.on_reaction if with_reactions else None
    sender._on_emoji_created = recorder.on_emoji_created if with_emoji else None
    sender._on_emoji_deleted = recorder.on_emoji_deleted if with_emoji else None
    return sender


def _stoat_message(*, channel, author, content="hi", id="m1"):
    return SimpleNamespace(channel=channel, author=author, content=content, id=id)


# ---------------------------------------------------------------- _handle_message


async def test_handle_message_ignores_a_bot_author():
    recorder = _Recorder()
    sender = _make_sender(recorder, FakeClient())
    author = FakeAuthor(id="u1", bot=True)

    await sender._handle_message(_stoat_message(channel=FakeChannel(id="42"), author=author))

    assert recorder.messages == []


async def test_handle_message_status_command_replies_in_channel_and_doesnt_relay():
    recorder = _Recorder()
    sender = _make_sender(recorder, FakeClient())
    channel = FakeChannel(id="42")
    author = FakeAuthor(id="u1")

    await sender._handle_message(_stoat_message(channel=channel, author=author, content="/status"))

    assert recorder.messages == []
    assert len(channel.sent) == 1
    assert "Stoat" in channel.sent[0]["content"]


async def test_handle_message_linked_channels_command_routes_to_its_handler():
    # Full behavior (a configured linker, admin gating - there is none) is
    # covered in test_stoat_admin_dispatch.py; this only proves _handle_message
    # actually routes "/linked-channels" there instead of relaying it.
    recorder = _Recorder()
    sender = _make_sender(recorder, FakeClient())
    channel = FakeChannel(id="42")
    author = FakeAuthor(id="u1")

    await sender._handle_message(_stoat_message(channel=channel, author=author, content="/linked-channels"))

    assert recorder.messages == []
    assert channel.sent == [{"content": "Linking isn't configured.", "masquerade": None}]


async def test_handle_message_dispatches_a_standard_message_with_a_cached_avatar():
    recorder = _Recorder()
    client = FakeClient()
    sender = _make_sender(recorder, client)
    channel = FakeChannel(id="42", name="general")
    author = FakeAuthor(id="u1", tag="alice#0000", display_name="Alice", avatar=FakeAsset("https://cdn.example/a.png"))

    await sender._handle_message(_stoat_message(channel=channel, author=author, content="hello", id="m1"))

    [message] = recorder.messages
    assert message.origin_connector_id == "stoat"
    assert message.origin_channel_id == "42"
    assert message.channel_name == "general"
    assert message.sender_name == "Alice"
    assert message.sender_avatar_url == "https://cdn.example/a.png"
    assert message.content_markdown == "hello"
    assert message.message_id == "m1"


async def test_handle_message_falls_back_to_tag_when_no_display_name():
    recorder = _Recorder()
    client = FakeClient()
    sender = _make_sender(recorder, client)
    channel = FakeChannel(id="42")
    author = FakeAuthor(id="u1", tag="alice#0000", display_name=None)

    await sender._handle_message(_stoat_message(channel=channel, author=author))

    assert recorder.messages[0].sender_name == "alice#0000"


async def test_handle_message_fetches_a_fresh_member_when_the_avatar_is_uncached():
    recorder = _Recorder()
    client = FakeClient()
    sender = _make_sender(recorder, client)
    channel = FakeChannel(id="42", server_id="srv-1")
    author = FakeAuthor(id="u1", avatar=None, server_avatar=None)
    server = client.add_server(FakeServer(id="srv-1"))
    server.add_member("u1", FakeAuthor(id="u1", avatar=FakeAsset("https://cdn.example/fresh.png")))

    await sender._handle_message(_stoat_message(channel=channel, author=author))

    assert recorder.messages[0].sender_avatar_url == "https://cdn.example/fresh.png"


# ---------------------------------------------------------------- _handle_reaction


async def test_handle_reaction_dispatches_add_and_remove():
    recorder = _Recorder()
    sender = _make_sender(recorder, FakeClient())
    message = _stoat_message(channel=FakeChannel(id="42"), author=FakeAuthor(id="u1"), id="m1")

    await sender._handle_reaction(message, "other-user", "\U0001f600", added=True)
    await sender._handle_reaction(message, "other-user", "\U0001f600", added=False)

    assert [r.added for r in recorder.reactions] == [True, False]
    assert recorder.reactions[0].origin_channel_id == "42"
    assert recorder.reactions[0].origin_message_id == "m1"
    assert recorder.reactions[0].emoji == "\U0001f600"


async def test_handle_reaction_drops_the_bridges_own_echoed_reaction():
    recorder = _Recorder()
    sender = _make_sender(recorder, FakeClient(), self_id="bridge-bot-id")
    message = _stoat_message(channel=FakeChannel(id="42"), author=FakeAuthor(id="u1"))

    await sender._handle_reaction(message, "bridge-bot-id", "\U0001f600", added=True)

    assert recorder.reactions == []


async def test_handle_reaction_is_a_noop_when_reactions_arent_wired_up():
    recorder = _Recorder()
    sender = _make_sender(recorder, FakeClient(), with_reactions=False)
    message = _stoat_message(channel=FakeChannel(id="42"), author=FakeAuthor(id="u1"))

    await sender._handle_reaction(message, "other-user", "\U0001f600", added=True)  # must not raise

    assert recorder.reactions == []


async def test_handle_reaction_with_a_custom_emoji_id():
    recorder = _Recorder()
    sender = _make_sender(recorder, FakeClient())
    message = _stoat_message(channel=FakeChannel(id="42"), author=FakeAuthor(id="u1"))

    await sender._handle_reaction(message, "other-user", "01ARZ3NDEKTSV4RRFFQ69G5FAV", added=True)

    [reaction] = recorder.reactions
    assert reaction.emoji.native_id == "01ARZ3NDEKTSV4RRFFQ69G5FAV"


# ---------------------------------------------------------------- on_emoji_create / on_emoji_delete


async def test_on_emoji_create_dispatches():
    recorder = _Recorder()
    sender = _make_sender(recorder, FakeClient())
    emoji = SimpleNamespace(id="e1", name="smile", image=FakeAsset("https://cdn.example/e1.png"), animated=False, creator_id=None)

    await sender.on_emoji_create(emoji)

    [created] = recorder.emoji_created
    assert created.emoji.native_id == "e1"
    assert created.emoji.image_url == "https://cdn.example/e1.png"


async def test_on_emoji_create_drops_the_bridges_own_mirrored_emoji():
    recorder = _Recorder()
    sender = _make_sender(recorder, FakeClient(), self_id="bridge-bot-id")
    emoji = SimpleNamespace(
        id="e1", name="smile", image=FakeAsset("https://cdn.example/e1.png"), animated=False, creator_id="bridge-bot-id"
    )

    await sender.on_emoji_create(emoji)

    assert recorder.emoji_created == []


async def test_on_emoji_create_is_a_noop_when_emoji_sync_isnt_wired_up():
    recorder = _Recorder()
    sender = _make_sender(recorder, FakeClient(), with_emoji=False)
    emoji = SimpleNamespace(id="e1", name="smile", image=FakeAsset("https://cdn.example/e1.png"), animated=False, creator_id=None)

    await sender.on_emoji_create(emoji)  # must not raise

    assert recorder.emoji_created == []


async def test_on_emoji_delete_dispatches():
    recorder = _Recorder()
    sender = _make_sender(recorder, FakeClient())
    emoji = SimpleNamespace(id="e1")

    await sender.on_emoji_delete(emoji)

    [deleted] = recorder.emoji_deleted
    assert deleted.native_id == "e1"


async def test_on_emoji_delete_is_a_noop_when_emoji_sync_isnt_wired_up():
    recorder = _Recorder()
    sender = _make_sender(recorder, FakeClient(), with_emoji=False)
    emoji = SimpleNamespace(id="e1")

    await sender.on_emoji_delete(emoji)  # must not raise

    assert recorder.emoji_deleted == []
