"""Tests for StoatSenderService's gateway-event handlers - _handle_message
(relay, avatar resolution, and skipping messages the ext.commands processor
already claimed), _handle_message_react, and
_handle_emoji_create/_handle_emoji_delete - against the fake_stoat scaffolding.

Constructs the service via object.__new__ rather than StoatSenderService(...)
directly, same as test_stoat_resolve_avatar.py: __init__ builds a
_StoatClient whose constructor makes a real network call
(_discover_websocket_base), which none of these handlers actually need.
"""

from __future__ import annotations

from collections import deque
from types import SimpleNamespace

import stoat

from stoat_discord_bridge.models import CustomEmoji
from stoat_discord_bridge.services.caching import AsyncTTLCache
from stoat_discord_bridge.services.stoat_service import StoatSenderService
from stoat_discord_bridge.status import HealthTracker
from tests.fakes.fake_stoat import FakeAsset, FakeAuthor, FakeChannel, FakeClient, FakeServer


class _Recorder:
    def __init__(self) -> None:
        self.messages: list = []
        self.reactions: list = []
        self.emoji_created: list = []
        self.emoji_deleted: list = []
        self.pins: list = []
        self.typing: list = []
        self.edits: list = []

    async def on_message(self, message) -> None:
        self.messages.append(message)

    async def on_pin(self, pin) -> None:
        self.pins.append(pin)

    async def on_edit(self, edit) -> None:
        self.edits.append(edit)

    async def on_typing(self, typing) -> None:
        self.typing.append(typing)

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
    sender.server_id = "srv-1"
    sender._client = client
    sender._config = SimpleNamespace(label="Stoat", pronoun_forwarding=False, color_forwarding=True)
    sender._pronoun_cache = AsyncTTLCache(600.0)
    sender._health = HealthTracker({"stoat": "Stoat"})
    sender._linker = None
    sender._user_linker = None
    sender._self_id = self_id
    sender._command_message_ids = deque(maxlen=512)
    sender._on_message = recorder.on_message
    sender._on_reaction = recorder.on_reaction if with_reactions else None
    sender._on_emoji_created = recorder.on_emoji_created if with_emoji else None
    sender._on_emoji_deleted = recorder.on_emoji_deleted if with_emoji else None
    sender._on_pin = recorder.on_pin
    sender._on_typing = recorder.on_typing
    sender._on_edit = recorder.on_edit
    return sender


def _stoat_message(*, channel, author, content="hi", id="m1", attachments=None):
    return SimpleNamespace(
        channel=channel, author=author, content=content, id=id, attachments=attachments or []
    )


# ---------------------------------------------------------------- _handle_message


async def test_handle_message_ignores_a_bot_author():
    recorder = _Recorder()
    sender = _make_sender(recorder, FakeClient())
    author = FakeAuthor(id="u1", bot=True)

    await sender._handle_message(_stoat_message(channel=FakeChannel(id="42"), author=author))

    assert recorder.messages == []


async def test_handle_message_pin_system_event_emits_a_pin_and_doesnt_relay():
    recorder = _Recorder()
    sender = _make_sender(recorder, FakeClient())
    channel = FakeChannel(id="42")
    author = FakeAuthor(id="u1")
    message = SimpleNamespace(
        channel=channel,
        author=author,
        content="",
        id="sys1",
        system_event=stoat.MessagePinnedSystemEvent(pinned_message_id="pm1", internal_by="u1", message=None),
    )

    await sender._handle_message(message)

    assert recorder.messages == []
    [pin] = recorder.pins
    assert (pin.origin_connector_id, pin.origin_channel_id, pin.origin_message_id, pin.pinned) == (
        "stoat",
        "42",
        "pm1",
        True,
    )


async def test_handle_message_unpin_system_event_emits_an_unpin():
    recorder = _Recorder()
    sender = _make_sender(recorder, FakeClient())
    channel = FakeChannel(id="42")
    message = SimpleNamespace(
        channel=channel,
        author=FakeAuthor(id="u1"),
        content="",
        id="sys2",
        system_event=stoat.MessageUnpinnedSystemEvent(unpinned_message_id="um1", internal_by="u1", message=None),
    )

    await sender._handle_message(message)

    assert recorder.messages == []
    [pin] = recorder.pins
    assert (pin.origin_message_id, pin.pinned) == ("um1", False)


async def test_handle_message_skips_a_message_the_command_processor_claimed():
    # `/link channel …`, `/status`, … are handled by the ext.commands processor
    # on _StoatClient off the same MessageCreateEvent; it records the id so
    # _handle_message (driven via call_object_handlers_hook) doesn't also relay
    # the invocation as chat.
    recorder = _Recorder()
    sender = _make_sender(recorder, FakeClient())
    channel = FakeChannel(id="42")
    author = FakeAuthor(id="u1")
    sender._command_message_ids.append("m1")

    await sender._handle_message(
        _stoat_message(channel=channel, author=author, content="/status", id="m1")
    )

    assert recorder.messages == []
    assert channel.sent == []


async def test_handle_message_skips_a_recorded_command_reply():
    # _reply flags the bot's own command output the same way, so it's dropped
    # even though it's authored by the bridge.
    recorder = _Recorder()
    sender = _make_sender(recorder, FakeClient())
    channel = FakeChannel(id="42")
    author = FakeAuthor(id="u1")
    sender._command_message_ids.append("reply-7")

    await sender._handle_message(
        _stoat_message(channel=channel, author=author, content="linked ok", id="reply-7")
    )

    assert recorder.messages == []


async def test_handle_message_relays_a_slash_prefixed_non_command():
    # A message that merely starts with "/" but isn't one of our commands is
    # normal chat - the command processor never claimed it, so it relays.
    recorder = _Recorder()
    sender = _make_sender(recorder, FakeClient())
    channel = FakeChannel(id="42", name="general")
    author = FakeAuthor(id="u1", display_name="Alice")

    await sender._handle_message(
        _stoat_message(channel=channel, author=author, content="/shrug whatever", id="m9")
    )

    [message] = recorder.messages
    assert message.content_markdown == "/shrug whatever"


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
    assert message.source_label == "Stoat"
    assert message.sender_name == "Alice"
    assert message.sender_avatar_url == "https://cdn.example/a.png"
    assert message.sender_user_id == "u1"
    assert message.content_markdown == "hello"
    assert message.message_id == "m1"


async def test_handle_message_resolves_channel_mentions_to_names():
    recorder = _Recorder()
    client = FakeClient()
    client.add_channel(FakeChannel(id="01ARZ3NDEKTSV4RRFFQ69G5FAV", name="off-topic"))
    sender = _make_sender(recorder, client)
    channel = FakeChannel(id="42", name="general")
    author = FakeAuthor(id="u1", tag="alice#0000", display_name="Alice", avatar=None)

    await sender._handle_message(
        _stoat_message(channel=channel, author=author, content="see <#01ARZ3NDEKTSV4RRFFQ69G5FAV>", id="m1")
    )

    [message] = recorder.messages
    assert message.mentioned_channels == {"01ARZ3NDEKTSV4RRFFQ69G5FAV": "off-topic"}


async def test_handle_message_falls_back_to_the_bare_username_when_no_display_name():
    # the discriminator suffix (tag = "name#0000") is stripped for a
    # masquerade name - it reads as broken/internal even though accurate.
    recorder = _Recorder()
    client = FakeClient()
    sender = _make_sender(recorder, client)
    channel = FakeChannel(id="42")
    author = FakeAuthor(id="u1", name="alice", tag="alice#0000", display_name=None)

    await sender._handle_message(_stoat_message(channel=channel, author=author))

    assert recorder.messages[0].sender_name == "alice"


async def test_handle_message_prefers_the_members_nick_over_the_account_display_name():
    # stoat.py's Member.display_name ignores the member's own per-server
    # nick and passes straight through to the underlying User's
    # account-level display_name (confirmed against the installed package)
    # - _handle_message must check nick itself.
    recorder = _Recorder()
    client = FakeClient()
    sender = _make_sender(recorder, client)
    channel = FakeChannel(id="42")
    author = FakeAuthor(id="u1", tag="alice#0000", display_name="Global Alice", nick="Server Nickname")

    await sender._handle_message(_stoat_message(channel=channel, author=author))

    assert recorder.messages[0].sender_name == "Server Nickname"


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


async def test_handle_message_maps_attachments_onto_the_standard_message():
    # An image-only Stoat message: empty content, one CDN attachment. Without
    # mapping it, every receiver relays a blank body.
    recorder = _Recorder()
    sender = _make_sender(recorder, FakeClient())
    channel = FakeChannel(id="42", name="general")
    author = FakeAuthor(id="u1", display_name="Alice")
    attachment = SimpleNamespace(
        url=lambda: "https://cdn.example/f.png",
        filename="f.png",
        content_type="image/png",
        size=10,
    )

    await sender._handle_message(
        _stoat_message(channel=channel, author=author, content="", attachments=[attachment])
    )

    [message] = recorder.messages
    assert [a.url for a in message.attachments] == ["https://cdn.example/f.png"]
    assert message.attachments[0].filename == "f.png"
    assert message.attachments[0].content_type == "image/png"
    assert message.attachments[0].size_bytes == 10


# ---------------------------------------------------------------- _handle_typing


async def test_handle_typing_emits_a_standard_typing():
    recorder = _Recorder()
    sender = _make_sender(recorder, FakeClient())

    await sender._handle_typing(SimpleNamespace(channel_id="c1", user_id="u1"))

    assert [(t.origin_channel_id, t.sender_user_id) for t in recorder.typing] == [("c1", "u1")]
    assert recorder.typing[0].active is True


async def test_handle_typing_emits_an_inactive_standard_typing_on_stop():
    recorder = _Recorder()
    sender = _make_sender(recorder, FakeClient())

    await sender._handle_typing(SimpleNamespace(channel_id="c1", user_id="u1"), active=False)

    assert [(t.origin_channel_id, t.active) for t in recorder.typing] == [("c1", False)]


async def test_handle_typing_drops_the_bridges_own_echoed_typing():
    recorder = _Recorder()
    sender = _make_sender(recorder, FakeClient(), self_id="bridge-bot-id")

    await sender._handle_typing(SimpleNamespace(channel_id="c1", user_id="bridge-bot-id"))

    assert recorder.typing == []


async def test_handle_typing_is_a_noop_when_typing_isnt_wired_up():
    recorder = _Recorder()
    sender = _make_sender(recorder, FakeClient())
    sender._on_typing = None

    await sender._handle_typing(SimpleNamespace(channel_id="c1", user_id="u1"))  # must not raise

    assert recorder.typing == []


# ---------------------------------------------------------------- _handle_message_react


def _react_event(*, channel_id="42", message_id="m1", user_id="other-user", emoji="\U0001f600", message=None):
    return SimpleNamespace(
        channel_id=channel_id, message_id=message_id, user_id=user_id, emoji=emoji, message=message
    )


async def test_handle_message_react_dispatches_add_and_remove():
    recorder = _Recorder()
    sender = _make_sender(recorder, FakeClient())

    await sender._handle_message_react(_react_event(), added=True)
    await sender._handle_message_react(_react_event(), added=False)

    assert [r.added for r in recorder.reactions] == [True, False]
    assert recorder.reactions[0].origin_channel_id == "42"
    assert recorder.reactions[0].origin_message_id == "m1"
    assert recorder.reactions[0].emoji == "\U0001f600"
    assert recorder.reactions[0].origin_reactor_count is None


async def test_handle_message_react_drops_the_bridges_own_echoed_reaction():
    recorder = _Recorder()
    sender = _make_sender(recorder, FakeClient(), self_id="bridge-bot-id")

    await sender._handle_message_react(_react_event(user_id="bridge-bot-id"), added=True)

    assert recorder.reactions == []


async def test_handle_message_react_is_a_noop_when_reactions_arent_wired_up():
    recorder = _Recorder()
    sender = _make_sender(recorder, FakeClient(), with_reactions=False)

    await sender._handle_message_react(_react_event(), added=True)  # must not raise

    assert recorder.reactions == []


async def test_handle_message_react_with_a_custom_emoji_ulid():
    recorder = _Recorder()
    sender = _make_sender(recorder, FakeClient())

    await sender._handle_message_react(_react_event(emoji="01ARZ3NDEKTSV4RRFFQ69G5FAV"), added=True)

    [reaction] = recorder.reactions
    assert reaction.emoji.native_id == "01ARZ3NDEKTSV4RRFFQ69G5FAV"


async def test_handle_message_react_drops_a_stoat_builtin_shortcode():
    recorder = _Recorder()
    sender = _make_sender(recorder, FakeClient())

    await sender._handle_message_react(_react_event(emoji="distorted_face"), added=True)

    assert recorder.reactions == []


async def test_handle_message_react_carries_the_origin_reactor_count():
    recorder = _Recorder()
    sender = _make_sender(recorder, FakeClient())
    message = SimpleNamespace(reactions={"\U0001f600": ("u1", "u2")})

    await sender._handle_message_react(_react_event(message=message), added=True)

    [reaction] = recorder.reactions
    assert reaction.origin_reactor_count == 2


# ---------------------------------------------------------------- _handle_emoji_create / _handle_emoji_delete


async def test_handle_emoji_create_dispatches():
    recorder = _Recorder()
    sender = _make_sender(recorder, FakeClient())
    emoji = SimpleNamespace(id="e1", name="smile", image=FakeAsset("https://cdn.example/e1.png"), animated=False, creator_id=None)

    await sender._handle_emoji_create(emoji)

    [created] = recorder.emoji_created
    assert created.emoji.native_id == "e1"
    assert created.emoji.image_url == "https://cdn.example/e1.png"


async def test_handle_emoji_create_drops_the_bridges_own_mirrored_emoji():
    recorder = _Recorder()
    sender = _make_sender(recorder, FakeClient(), self_id="bridge-bot-id")
    emoji = SimpleNamespace(
        id="e1", name="smile", image=FakeAsset("https://cdn.example/e1.png"), animated=False, creator_id="bridge-bot-id"
    )

    await sender._handle_emoji_create(emoji)

    assert recorder.emoji_created == []


async def test_handle_emoji_create_is_a_noop_when_emoji_sync_isnt_wired_up():
    recorder = _Recorder()
    sender = _make_sender(recorder, FakeClient(), with_emoji=False)
    emoji = SimpleNamespace(id="e1", name="smile", image=FakeAsset("https://cdn.example/e1.png"), animated=False, creator_id=None)

    await sender._handle_emoji_create(emoji)  # must not raise

    assert recorder.emoji_created == []


async def test_handle_emoji_delete_dispatches():
    recorder = _Recorder()
    sender = _make_sender(recorder, FakeClient())

    await sender._handle_emoji_delete("e1")

    [deleted] = recorder.emoji_deleted
    assert deleted.native_id == "e1"


async def test_handle_emoji_delete_is_a_noop_when_emoji_sync_isnt_wired_up():
    recorder = _Recorder()
    sender = _make_sender(recorder, FakeClient(), with_emoji=False)

    await sender._handle_emoji_delete("e1")  # must not raise

    assert recorder.emoji_deleted == []


# ---------------------------------------------------------------- _handle_message_update


def _update_event(*, after=None, message=None):
    return SimpleNamespace(after=after, message=message)


def _partial(*, content=stoat.UNDEFINED, channel_id="chan-1", id="m1"):
    ns = SimpleNamespace(channel_id=channel_id, id=id)
    if content is not stoat.UNDEFINED:
        ns.content = content
    return ns


async def test_handle_message_update_emits_an_edit_when_the_content_changed():
    recorder = _Recorder()
    sender = _make_sender(recorder, FakeClient())
    after = SimpleNamespace(author=FakeAuthor(id="u1", bot=False), mentions=[])

    await sender._handle_message_update(
        _update_event(message=_partial(content="fixed typo"), after=after)
    )

    assert [(e.origin_channel_id, e.origin_message_id, e.new_content_markdown) for e in recorder.edits] == [
        ("chan-1", "m1", "fixed typo")
    ]


async def test_handle_message_update_drops_a_bot_authored_edit_echo():
    recorder = _Recorder()
    sender = _make_sender(recorder, FakeClient())
    after = SimpleNamespace(author=FakeAuthor(id="bot", bot=True), mentions=[])

    await sender._handle_message_update(_update_event(message=_partial(content="x"), after=after))

    assert recorder.edits == []


async def test_handle_message_update_ignores_an_update_that_didnt_change_content():
    recorder = _Recorder()
    sender = _make_sender(recorder, FakeClient())

    # a pin / reaction / embed update: the partial carries no `content` field
    await sender._handle_message_update(_update_event(message=_partial()))

    assert recorder.edits == []


# ---------------------------------------------------------------- get_user_name


async def test_get_user_name_prefers_display_name():
    client = FakeClient()
    client.add_user("01KH", FakeAuthor(id="01KH", tag="shriner#0000", display_name="ShrinerH"))
    sender = _make_sender(_Recorder(), client)

    assert await sender.get_user_name("01KH") == "ShrinerH"


async def test_get_user_name_falls_back_to_tag_when_no_display_name():
    client = FakeClient()
    client.add_user("01KH", FakeAuthor(id="01KH", tag="shriner#0000", display_name=None))
    sender = _make_sender(_Recorder(), client)

    assert await sender.get_user_name("01KH") == "shriner#0000"


async def test_get_user_name_returns_none_when_the_fetch_fails():
    client = FakeClient()  # no user added - fetch_user raises LookupError
    sender = _make_sender(_Recorder(), client)

    assert await sender.get_user_name("unknown") is None
