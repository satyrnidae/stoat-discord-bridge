"""Tests for DiscordSenderService's gateway-event handlers - _handle_message,
_handle_raw_reaction/_is_other_bot, and _handle_guild_emojis_update - against
the fake_discord scaffolding. Builds a real DiscordSenderService (safe: its
constructor does no network I/O, same as test_discord_service.py) and swaps
its `_client` for a FakeClient so these handlers read fake gateway state
instead of a live discord.py cache.
"""

from __future__ import annotations

from types import SimpleNamespace

import discord

from stoat_discord_bridge.admin_commands import ChannelLinker, ConnectorInfo
from stoat_discord_bridge.config import DiscordConnectorConfig
from stoat_discord_bridge.services.discord_service import DiscordSenderService
from stoat_discord_bridge.status import HealthTracker
from stoat_discord_bridge.storage.channel_mappings import ChannelMappingRepository
from tests.fakes.fake_discord import (
    FakeAsset,
    FakeAttachment,
    FakeChannel,
    FakeClient,
    FakeGuild,
    FakePartialEmoji,
    FakeRawReactionActionEvent,
    FakeThread,
    FakeUser,
)


def _discord_config(**overrides):
    defaults = dict(id="discord", label="Discord", guild_id=123, bot_token="fake-token")
    defaults.update(overrides)
    return DiscordConnectorConfig(**defaults)


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
    recorder: _Recorder, client: FakeClient, *, linker=None, category_linker=None, **config_overrides
) -> DiscordSenderService:
    sender = DiscordSenderService(
        _discord_config(**config_overrides),
        on_message=recorder.on_message,
        health=HealthTracker({"discord": "Discord"}),
        on_reaction=recorder.on_reaction,
        on_emoji_created=recorder.on_emoji_created,
        on_emoji_deleted=recorder.on_emoji_deleted,
        on_pin=recorder.on_pin,
        on_typing=recorder.on_typing,
        on_edit=recorder.on_edit,
        linker=linker,
        category_linker=category_linker,
    )
    sender._client = client
    return sender


def _discord_message(
    *, channel, guild, author, content="hi", id=1, attachments=None, type=discord.MessageType.default, thread=None,
    mentions=None, channel_mentions=None,
):
    return SimpleNamespace(
        channel=channel, guild=guild, author=author, content=content, id=id, attachments=attachments or [],
        type=type, thread=thread, mentions=mentions or [], channel_mentions=channel_mentions or [],
    )


# ---------------------------------------------------------------- _handle_message


async def test_handle_message_ignores_a_bot_author():
    recorder = _Recorder()
    client = FakeClient()
    sender = _make_sender(recorder, client)
    guild = FakeGuild(id=123)
    channel = FakeChannel(id=42)
    author = FakeUser(id=1, bot=True)

    await sender._handle_message(_discord_message(channel=channel, guild=guild, author=author))

    assert recorder.messages == []


async def test_handle_message_ignores_a_message_outside_the_configured_guild():
    recorder = _Recorder()
    client = FakeClient()
    sender = _make_sender(recorder, client)
    channel = FakeChannel(id=42)
    author = FakeUser(id=1)

    await sender._handle_message(_discord_message(channel=channel, guild=None, author=author))
    await sender._handle_message(_discord_message(channel=channel, guild=FakeGuild(id=999), author=author))

    assert recorder.messages == []


async def test_handle_message_dispatches_a_standard_message():
    recorder = _Recorder()
    client = FakeClient()
    sender = _make_sender(recorder, client)
    guild = FakeGuild(id=123)
    channel = FakeChannel(id=42, name="general")
    author = FakeUser(id=1, display_name="Alice", display_avatar=FakeAsset("https://cdn.example/alice.png"))
    attachment = FakeAttachment(url="https://cdn.example/f.png", filename="f.png", content_type="image/png", size=10)

    await sender._handle_message(
        _discord_message(channel=channel, guild=guild, author=author, content="hello", id=99, attachments=[attachment])
    )

    [message] = recorder.messages
    assert message.origin_connector_id == "discord"
    assert message.origin_channel_id == "42"
    assert message.channel_name == "general"
    assert message.sender_name == "Alice"
    assert message.sender_avatar_url == "https://cdn.example/alice.png"
    assert message.sender_user_id == "1"
    assert message.content_markdown == "hello"
    assert message.message_id == "99"
    assert message.source_label == "Discord"
    assert [a.url for a in message.attachments] == ["https://cdn.example/f.png"]


async def test_handle_message_maps_channel_mentions_by_id_to_name():
    recorder = _Recorder()
    client = FakeClient()
    sender = _make_sender(recorder, client)
    guild = FakeGuild(id=123)
    channel = FakeChannel(id=42, name="general")
    author = FakeUser(id=1, display_name="Alice", display_avatar=FakeAsset("https://cdn.example/alice.png"))

    await sender._handle_message(
        _discord_message(
            channel=channel, guild=guild, author=author, content="see <#77>",
            channel_mentions=[SimpleNamespace(id=77, name="off-topic")],
        )
    )

    [message] = recorder.messages
    assert message.mentioned_channels == {"77": "off-topic"}


async def test_handle_message_uses_none_avatar_when_the_author_has_no_avatar():
    recorder = _Recorder()
    client = FakeClient()
    sender = _make_sender(recorder, client)
    guild = FakeGuild(id=123)
    channel = FakeChannel(id=42)
    author = FakeUser(id=1, display_avatar=None)

    await sender._handle_message(_discord_message(channel=channel, guild=guild, author=author))

    assert recorder.messages[0].sender_avatar_url is None


async def test_handle_message_suppresses_the_pins_add_system_message():
    recorder = _Recorder()
    sender = _make_sender(recorder, FakeClient())
    guild = FakeGuild(id=123)
    channel = FakeChannel(id=42, name="general")
    author = FakeUser(id=1, display_name="Alice")

    await sender._handle_message(
        _discord_message(
            channel=channel, guild=guild, author=author, content="", id=9,
            type=discord.MessageType.pins_add,
        )
    )

    assert recorder.messages == []


# ---------------------------------------------------------------- _handle_raw_message_edit


def _edit_payload(**overrides):
    defaults = dict(guild_id=123, channel_id=42, message_id=7, data={})
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


async def test_handle_raw_message_edit_emits_a_pin_when_pinned_toggles():
    recorder = _Recorder()
    sender = _make_sender(recorder, FakeClient())

    await sender._handle_raw_message_edit(_edit_payload(data={"pinned": True}))
    await sender._handle_raw_message_edit(_edit_payload(data={"pinned": False}))

    assert [(p.origin_channel_id, p.origin_message_id, p.pinned) for p in recorder.pins] == [
        ("42", "7", True),
        ("42", "7", False),
    ]


async def test_handle_raw_message_edit_ignores_an_auto_embed_update():
    # A link Discord just unfurled: `content` present (unchanged) but no
    # `edited_timestamp` - must not tag the relayed copies "(edited)".
    recorder = _Recorder()
    sender = _make_sender(recorder, FakeClient())

    await sender._handle_raw_message_edit(_edit_payload(data={"content": "edited", "embeds": [{}]}))

    assert recorder.pins == []
    assert recorder.edits == []


async def test_handle_raw_message_edit_ignores_a_different_guild():
    recorder = _Recorder()
    sender = _make_sender(recorder, FakeClient())

    await sender._handle_raw_message_edit(_edit_payload(guild_id=999, data={"pinned": True}))

    assert recorder.pins == []


async def test_handle_raw_message_edit_emits_an_edit_on_a_real_content_edit():
    recorder = _Recorder()
    sender = _make_sender(recorder, FakeClient())

    await sender._handle_raw_message_edit(
        _edit_payload(
            data={
                "content": "fixed typo",
                "edited_timestamp": "2026-09-03T00:00:00+00:00",
                "author": {"id": "5", "bot": False},
            },
            message=SimpleNamespace(mentions=[SimpleNamespace(id=9, display_name="Bob")]),
        )
    )

    assert [
        (e.origin_channel_id, e.origin_message_id, e.new_content_markdown, e.mentioned_users)
        for e in recorder.edits
    ] == [("42", "7", "fixed typo", {"9": "Bob"})]


async def test_handle_raw_message_edit_drops_our_own_webhook_copy_being_edited():
    recorder = _Recorder()
    sender = _make_sender(recorder, FakeClient())

    # cache-free detection: webhook_id in the raw payload, message uncached
    await sender._handle_raw_message_edit(
        _edit_payload(
            data={"content": "x", "edited_timestamp": "2026-09-03T00:00:00+00:00", "webhook_id": "123"}
        )
    )

    assert recorder.edits == []


# ---------------------------------------------------------------- _handle_typing


def _typing_channel(id=42, guild_id=123):
    return SimpleNamespace(id=id, guild=FakeGuild(id=guild_id))


async def test_handle_typing_emits_a_standard_typing():
    recorder = _Recorder()
    sender = _make_sender(recorder, FakeClient())

    await sender._handle_typing(_typing_channel(), FakeUser(id=7, display_name="Alice"))

    assert [(t.origin_channel_id, t.sender_name, t.sender_user_id) for t in recorder.typing] == [
        ("42", "Alice", "7")
    ]


async def test_handle_typing_ignores_dms_and_other_guilds():
    recorder = _Recorder()
    sender = _make_sender(recorder, FakeClient())

    await sender._handle_typing(SimpleNamespace(id=42, guild=None), FakeUser(id=7))
    await sender._handle_typing(_typing_channel(guild_id=999), FakeUser(id=7))

    assert recorder.typing == []


async def test_handle_typing_ignores_the_bridge_bot_and_other_bots():
    recorder = _Recorder()
    client = FakeClient(user=FakeUser(id=1))
    sender = _make_sender(recorder, client)

    await sender._handle_typing(_typing_channel(), FakeUser(id=1))  # the bridge bot itself
    await sender._handle_typing(_typing_channel(), FakeUser(id=2, bot=True))

    assert recorder.typing == []


# ---------------------------------------------------------------- _handle_raw_reaction


def _reaction_payload(**overrides):
    defaults = dict(
        guild_id=123,
        channel_id=42,
        message_id=7,
        user_id=2,
        emoji=FakePartialEmoji(name="\U0001f600"),
        member=None,
    )
    defaults.update(overrides)
    return FakeRawReactionActionEvent(**defaults)


async def test_handle_raw_reaction_dispatches_add_and_remove():
    recorder = _Recorder()
    client = FakeClient(user=FakeUser(id=1))
    sender = _make_sender(recorder, client)

    await sender._handle_raw_reaction(_reaction_payload(), added=True)
    await sender._handle_raw_reaction(_reaction_payload(), added=False)

    assert [r.added for r in recorder.reactions] == [True, False]
    assert recorder.reactions[0].origin_channel_id == "42"
    assert recorder.reactions[0].origin_message_id == "7"
    assert recorder.reactions[0].emoji == "\U0001f600"


async def test_handle_raw_reaction_ignores_a_different_guild():
    recorder = _Recorder()
    client = FakeClient(user=FakeUser(id=1))
    sender = _make_sender(recorder, client)

    await sender._handle_raw_reaction(_reaction_payload(guild_id=999), added=True)

    assert recorder.reactions == []


async def test_handle_raw_reaction_drops_the_bridges_own_mirrored_reaction():
    recorder = _Recorder()
    client = FakeClient(user=FakeUser(id=1))
    sender = _make_sender(recorder, client)

    await sender._handle_raw_reaction(_reaction_payload(user_id=1), added=True)

    assert recorder.reactions == []


async def test_handle_raw_reaction_drops_another_bots_reaction_via_member():
    recorder = _Recorder()
    client = FakeClient(user=FakeUser(id=1))
    sender = _make_sender(recorder, client)

    await sender._handle_raw_reaction(_reaction_payload(member=FakeUser(id=5, bot=True)), added=True)

    assert recorder.reactions == []


async def test_handle_raw_reaction_drops_another_bots_reaction_removal_via_user_cache():
    recorder = _Recorder()
    client = FakeClient(user=FakeUser(id=1))
    client.add_user(FakeUser(id=5, bot=True))
    sender = _make_sender(recorder, client)

    # REACTION_REMOVE never carries `member` - falls back to the client's user cache.
    await sender._handle_raw_reaction(_reaction_payload(user_id=5, member=None), added=False)

    assert recorder.reactions == []


async def test_handle_raw_reaction_with_a_custom_emoji():
    recorder = _Recorder()
    client = FakeClient(user=FakeUser(id=1))
    sender = _make_sender(recorder, client)
    emoji = FakePartialEmoji(name="pepe", id=555, animated=True, url="https://cdn.example/pepe.png")

    await sender._handle_raw_reaction(_reaction_payload(emoji=emoji), added=True)

    [reaction] = recorder.reactions
    assert reaction.emoji.native_id == "555"
    assert reaction.emoji.name == "pepe"
    assert reaction.emoji.animated is True


async def test_handle_raw_reaction_carries_the_reactor_count():
    from tests.fakes.fake_discord import FakeChannel, FakeFullMessage, FakeReaction

    recorder = _Recorder()
    client = FakeClient(user=FakeUser(id=1))
    channel = client.add_channel(FakeChannel(id=42))
    channel.full_messages[7] = FakeFullMessage(7, reactions=[FakeReaction("\U0001f600", count=3, me=True)])
    sender = _make_sender(recorder, client)

    await sender._handle_raw_reaction(_reaction_payload(), added=True)

    assert recorder.reactions[0].origin_reactor_count == 3


async def test_handle_raw_reaction_reactor_count_is_none_when_the_fetch_fails():
    recorder = _Recorder()
    client = FakeClient(user=FakeUser(id=1))  # no channel registered -> fetch raises
    sender = _make_sender(recorder, client)

    await sender._handle_raw_reaction(_reaction_payload(), added=True)

    assert recorder.reactions[0].origin_reactor_count is None


# ---------------------------------------------------------------- _handle_guild_emojis_update


async def test_guild_emojis_update_ignores_a_different_guild():
    recorder = _Recorder()
    client = FakeClient()
    sender = _make_sender(recorder, client)

    await sender._handle_guild_emojis_update(FakeGuild(id=999), [], [SimpleNamespace(id=1)])

    assert recorder.emoji_created == []


async def test_guild_emojis_update_reports_a_newly_added_emoji():
    recorder = _Recorder()
    client = FakeClient()
    sender = _make_sender(recorder, client)
    new_emoji = SimpleNamespace(id=1, name="smile", url="https://cdn.example/e.png", animated=False, user=None)

    await sender._handle_guild_emojis_update(FakeGuild(id=123), [], [new_emoji])

    [created] = recorder.emoji_created
    assert created.emoji.native_id == "1"
    assert created.emoji.name == "smile"


async def test_guild_emojis_update_skips_an_emoji_mirrored_by_a_bot():
    recorder = _Recorder()
    client = FakeClient()
    sender = _make_sender(recorder, client)
    bot_user = SimpleNamespace(bot=True)
    new_emoji = SimpleNamespace(id=1, name="smile", url="https://cdn.example/e.png", animated=False, user=bot_user)

    await sender._handle_guild_emojis_update(FakeGuild(id=123), [], [new_emoji])

    assert recorder.emoji_created == []


async def test_guild_emojis_update_reports_a_removed_emoji():
    recorder = _Recorder()
    client = FakeClient()
    sender = _make_sender(recorder, client)
    old_emoji = SimpleNamespace(id=1, name="smile", url="https://cdn.example/e.png", animated=False, user=None)

    await sender._handle_guild_emojis_update(FakeGuild(id=123), [old_emoji], [])

    [deleted] = recorder.emoji_deleted
    assert deleted.native_id == "1"


async def test_guild_emojis_update_ignores_an_emoji_present_in_both_lists():
    recorder = _Recorder()
    client = FakeClient()
    sender = _make_sender(recorder, client)
    unchanged = SimpleNamespace(id=1, name="smile", url="https://cdn.example/e.png", animated=False, user=None)

    await sender._handle_guild_emojis_update(FakeGuild(id=123), [unchanged], [unchanged])

    assert recorder.emoji_created == []
    assert recorder.emoji_deleted == []


# ---------------------------------------------------------------- get_channel_name


async def test_get_channel_name_from_cache():
    client = FakeClient()
    client.add_channel(FakeChannel(id=42, name="general"))
    sender = _make_sender(_Recorder(), client)

    assert await sender.get_channel_name("42") == "general"


async def test_get_channel_name_returns_none_when_not_found():
    client = FakeClient()
    sender = _make_sender(_Recorder(), client)

    assert await sender.get_channel_name("999") is None


async def test_get_channel_name_returns_none_on_a_non_numeric_id():
    client = FakeClient()
    sender = _make_sender(_Recorder(), client)

    assert await sender.get_channel_name("not-a-number") is None


# ---------------------------------------------------------------- get_user_name


async def test_get_user_name_from_cache():
    client = FakeClient()
    client.add_user(FakeUser(id=216591124222050304, display_name="ShrinerH"))
    sender = _make_sender(_Recorder(), client)

    assert await sender.get_user_name("216591124222050304") == "ShrinerH"


async def test_get_user_name_returns_none_when_not_found():
    client = FakeClient()
    sender = _make_sender(_Recorder(), client)

    assert await sender.get_user_name("999") is None


async def test_get_user_name_returns_none_on_a_non_numeric_id():
    client = FakeClient()
    sender = _make_sender(_Recorder(), client)

    assert await sender.get_user_name("not-a-number") is None


# ---------------------------------------------------------------- get_thread_parent


async def test_get_thread_parent_returns_the_parent_id_and_name_for_a_thread():
    client = FakeClient()
    parent = FakeChannel(id=42, name="bot-config")
    client.add_channel(FakeThread(id=777, parent=parent, name="cool thread", guild=FakeGuild(id=123)))
    sender = _make_sender(_Recorder(), client)

    assert await sender.get_thread_parent("777") == ("42", "bot-config")


async def test_get_thread_parent_returns_none_for_a_plain_channel():
    client = FakeClient()
    client.add_channel(FakeChannel(id=42, name="general"))
    sender = _make_sender(_Recorder(), client)

    assert await sender.get_thread_parent("42") is None


async def test_get_thread_parent_returns_none_for_an_unresolvable_id():
    sender = _make_sender(_Recorder(), FakeClient())

    assert await sender.get_thread_parent("999") is None
    assert await sender.get_thread_parent("not-a-number") is None


# ---------------------------------------------------------------- _handle_thread_create


async def _stoat_ensure_channel(name: str, category: str | None = None, is_thread_category: bool = False, category_parent_channel_id: str | None = None) -> str:
    return f"stoat_{name}"


async def _irc_ensure_channel(name: str, category: str | None = None, is_thread_category: bool = False, category_parent_channel_id: str | None = None) -> str:
    return f"irc_{name}"


def _linked_connectors():
    return {
        "discord": ConnectorInfo(id="discord", label="Discord"),
        "stoat": ConnectorInfo(id="stoat", label="Stoat", ensure_channel=_stoat_ensure_channel),
        "irc": ConnectorInfo(id="irc", label="IRC", ensure_channel=_irc_ensure_channel),
    }


async def test_handle_thread_create_mirrors_and_relays_the_starter_message_as_the_user(fake_db):
    channel_mappings = ChannelMappingRepository(fake_db)
    linker = ChannelLinker(channel_mappings, _linked_connectors())
    await linker.link_channel(
        local_connector="discord", local_channel_id="42", local_channel_name="general",
        source="stoat", source_id="s-general", destination_id=None,
    )
    recorder = _Recorder()
    client = FakeClient(user=FakeUser(id=9, display_name="Bridge", display_avatar=FakeAsset("https://cdn.example/bot.png")))
    sender = _make_sender(recorder, client, linker=linker)
    parent = FakeChannel(id=42)
    author = FakeUser(id=1, display_name="isabel")
    guild = FakeGuild(id=123)
    thread = FakeThread(id=777, parent=parent, name="Test Thread", guild=guild)
    thread._starter_message = _discord_message(channel=thread, guild=guild, author=author, content="first!", id=555)

    await sender._handle_thread_create(thread)

    group = await channel_mappings.get_bridge_group("discord", "777")
    assert group is not None
    mapped = {m.connector_id: m.channel_id for m in await channel_mappings.get_mapped_channels(group)}
    assert mapped["stoat"] == "stoat_Test Thread"
    assert mapped["irc"] == "irc_Test Thread"

    # the thread's own starter message is relayed as the originating user
    # (not a synthetic bot announcement), into the freshly-linked channel...
    starter, notice = recorder.messages
    assert starter.origin_connector_id == "discord"
    assert starter.origin_channel_id == "777"
    assert starter.channel_name == "Test Thread"
    assert starter.content_markdown == "first!"
    assert starter.sender_name == "isabel"
    assert starter.message_id == "555"

    # ...and the parent channel gets a bot notice linking to the mirrored channel.
    assert notice.origin_channel_id == "42"
    assert notice.sender_name == "Bridge"
    assert notice.content_markdown == "isabel started a thread: <#777>"


async def test_handle_thread_create_marks_ready_when_the_starter_message_hasnt_arrived(fake_db):
    channel_mappings = ChannelMappingRepository(fake_db)
    linker = ChannelLinker(channel_mappings, _linked_connectors())
    await linker.link_channel(
        local_connector="discord", local_channel_id="42", local_channel_name="general",
        source="stoat", source_id="s-general", destination_id=None,
    )
    recorder = _Recorder()
    sender = _make_sender(recorder, FakeClient(), linker=linker)
    thread = FakeThread(id=777, parent=FakeChannel(id=42, name="general"), name="Test Thread", guild=FakeGuild(id=123))

    await sender._handle_thread_create(thread)

    # no starter message to relay yet - only the parent-channel bot notice is
    # posted, and the thread is flagged so _handle_message relays the next
    # in-thread message normally.
    [notice] = recorder.messages
    assert notice.origin_channel_id == "42"
    assert notice.content_markdown == "Someone started a thread: <#777>"
    assert 777 in sender._thread_ready
    assert 777 not in sender._pending_thread_starter


async def test_handle_thread_create_names_category_after_the_destinations_linked_parent(fake_db):
    calls = []

    async def stoat_ensure_channel(name, category=None, is_thread_category=False, category_parent_channel_id=None):
        calls.append((name, category, category_parent_channel_id))
        return f"stoat_{name}"

    async def resolve_channel_name(channel_id):
        return {"s-general": "Bot Config"}.get(channel_id)

    connectors = {
        "discord": ConnectorInfo(id="discord", label="Discord"),
        "stoat": ConnectorInfo(
            id="stoat", label="Stoat", ensure_channel=stoat_ensure_channel, resolve_channel_name=resolve_channel_name
        ),
    }
    channel_mappings = ChannelMappingRepository(fake_db)
    linker = ChannelLinker(channel_mappings, connectors)
    await linker.link_channel(
        local_connector="discord", local_channel_id="42", local_channel_name="general",
        source="stoat", source_id="s-general", destination_id=None,
    )
    recorder = _Recorder()
    client = FakeClient(user=FakeUser(id=9, display_name="Bridge", display_avatar=FakeAsset("https://cdn.example/bot.png")))
    sender = _make_sender(recorder, client, linker=linker)
    parent = FakeChannel(id=42, name="Announcements")
    thread = FakeThread(id=777, parent=parent, name="Test Thread", guild=FakeGuild(id=123))

    await sender._handle_thread_create(thread)

    # category = Stoat's own name for the linked parent channel, not the
    # Discord parent's name ("Announcements"); Stoat's own channel id for the
    # parent is forwarded too, to key the persistent thread-Category binding.
    assert calls == [("Test Thread", "Bot Config", "s-general")]


async def test_handle_thread_create_marks_destination_category_as_thread_category(fake_db):
    calls = []

    async def stoat_ensure_channel(name, category=None, is_thread_category=False, category_parent_channel_id=None):
        calls.append(is_thread_category)
        return f"stoat_{name}"

    connectors = {
        "discord": ConnectorInfo(id="discord", label="Discord"),
        "stoat": ConnectorInfo(id="stoat", label="Stoat", ensure_channel=stoat_ensure_channel),
    }
    channel_mappings = ChannelMappingRepository(fake_db)
    linker = ChannelLinker(channel_mappings, connectors)
    await linker.link_channel(
        local_connector="discord", local_channel_id="42", local_channel_name="general",
        source="stoat", source_id="s-general", destination_id=None,
    )
    recorder = _Recorder()
    client = FakeClient(user=FakeUser(id=9, display_name="Bridge", display_avatar=FakeAsset("https://cdn.example/bot.png")))
    sender = _make_sender(recorder, client, linker=linker)
    parent = FakeChannel(id=42, name="Announcements")
    thread = FakeThread(id=777, parent=parent, name="Test Thread", guild=FakeGuild(id=123))

    await sender._handle_thread_create(thread)

    # is_thread_category=True flows all the way from the thread-mirroring
    # call site through to ensure_channel, so the destination Category gets
    # marked as thread-only and /link-category will later refuse to link it.
    assert calls == [True]


async def test_handle_thread_create_skips_when_parent_isnt_bridged(fake_db):
    channel_mappings = ChannelMappingRepository(fake_db)
    linker = ChannelLinker(channel_mappings, _linked_connectors())
    recorder = _Recorder()
    client = FakeClient()
    sender = _make_sender(recorder, client, linker=linker)
    thread = FakeThread(id=777, parent=FakeChannel(id=42), name="Test Thread", guild=FakeGuild(id=123))

    await sender._handle_thread_create(thread)

    assert recorder.messages == []
    assert await channel_mappings.get_bridge_group("discord", "777") is None


async def test_handle_thread_create_ignores_a_different_guild(fake_db):
    channel_mappings = ChannelMappingRepository(fake_db)
    linker = ChannelLinker(channel_mappings, _linked_connectors())
    await linker.link_channel(
        local_connector="discord", local_channel_id="42", local_channel_name="general",
        source="stoat", source_id="s-general", destination_id=None,
    )
    recorder = _Recorder()
    sender = _make_sender(recorder, FakeClient(), linker=linker)
    thread = FakeThread(id=777, parent=FakeChannel(id=42), name="Test Thread", guild=FakeGuild(id=999))

    await sender._handle_thread_create(thread)

    assert recorder.messages == []


async def test_handle_thread_create_does_nothing_without_a_linker():
    recorder = _Recorder()
    sender = _make_sender(recorder, FakeClient())  # linker defaults to None
    thread = FakeThread(id=777, parent=FakeChannel(id=42), name="Test Thread", guild=FakeGuild(id=123))

    await sender._handle_thread_create(thread)

    assert recorder.messages == []


async def test_handle_thread_create_doesnt_relay_a_system_starter_message(fake_db):
    channel_mappings = ChannelMappingRepository(fake_db)
    linker = ChannelLinker(channel_mappings, _linked_connectors())
    await linker.link_channel(
        local_connector="discord", local_channel_id="42", local_channel_name="general",
        source="stoat", source_id="s-general", destination_id=None,
    )
    recorder = _Recorder()
    client = FakeClient(user=FakeUser(id=9, display_name="Bridge"))
    sender = _make_sender(recorder, client, linker=linker)
    guild = FakeGuild(id=123)
    parent = FakeChannel(id=42, name="general")
    author = FakeUser(id=1, display_name="isabel")
    thread = FakeThread(id=777, parent=parent, name="yet another test thread", guild=guild)
    # a standalone thread's "starter message" is the thread-created system row -
    # its content is just the thread name; it must not be relayed as user text.
    thread._starter_message = _discord_message(
        channel=thread, guild=guild, author=author, content="yet another test thread",
        id=555, type=discord.MessageType.thread_starter_message,
    )

    await sender._handle_thread_create(thread)

    # only the parent bot notice - the system starter row is dropped, and the
    # thread is flagged ready for the real first message.
    assert [m.content_markdown for m in recorder.messages] == ["isabel started a thread: <#777>"]
    assert 777 in sender._thread_ready


async def test_handle_message_buffers_the_starter_message_while_the_mirror_is_in_flight(fake_db):
    recorder = _Recorder()
    sender = _make_sender(recorder, FakeClient())
    guild = FakeGuild(id=123)
    thread = FakeThread(id=777, parent=FakeChannel(id=42), name="Test Thread", guild=guild)
    author = FakeUser(id=1, display_name="isabel")

    # simulate _handle_thread_create having started the mirror (thread id
    # recorded) but not yet finished when the starter message arrives.
    sender._pending_thread_starter[777] = None
    starter = _discord_message(channel=thread, guild=guild, author=author, content="first!")
    await sender._handle_message(starter)

    # not relayed yet - buffered for _handle_thread_create to flush once the
    # destination channel exists and is linked.
    assert recorder.messages == []
    assert sender._pending_thread_starter[777] is starter


async def test_handle_message_relays_the_first_thread_message_once_the_mirror_is_ready(fake_db):
    channel_mappings = ChannelMappingRepository(fake_db)
    linker = ChannelLinker(channel_mappings, _linked_connectors())
    await linker.link_channel(
        local_connector="discord", local_channel_id="42", local_channel_name="general",
        source="stoat", source_id="s-general", destination_id=None,
    )
    recorder = _Recorder()
    sender = _make_sender(recorder, FakeClient(), linker=linker)
    guild = FakeGuild(id=123)
    thread = FakeThread(id=777, parent=FakeChannel(id=42), name="Test Thread", guild=guild)
    author = FakeUser(id=1, display_name="isabel")

    await sender._handle_thread_create(thread)  # no starter yet -> 777 in _thread_ready (+ parent notice)
    await sender._handle_message(_discord_message(channel=thread, guild=guild, author=author, content="first!"))

    # the first in-thread message relays normally as the user, and the
    # one-shot _thread_ready flag is cleared.
    assert recorder.messages[-1].content_markdown == "first!"
    assert recorder.messages[-1].sender_name == "isabel"
    assert 777 not in sender._thread_ready


async def test_handle_message_suppresses_the_raw_parent_thread_created_system_message(fake_db):
    recorder = _Recorder()
    client = FakeClient(user=FakeUser(id=9, display_name="Bridge", display_avatar=FakeAsset("https://cdn.example/bot.png")))
    sender = _make_sender(recorder, client)
    guild = FakeGuild(id=123)
    parent = FakeChannel(id=42, name="general")
    author = FakeUser(id=1, display_name="isabel")

    await sender._handle_message(
        _discord_message(
            channel=parent, guild=guild, author=author, content="My New Thread",
            id=555, type=discord.MessageType.thread_created,
        )
    )

    # nothing relayed from _handle_message - the notice is _handle_thread_create's job
    assert recorder.messages == []


async def test_handle_thread_create_notice_links_to_the_mirrored_channel(fake_db):
    channel_mappings = ChannelMappingRepository(fake_db)
    linker = ChannelLinker(channel_mappings, _linked_connectors())
    await linker.link_channel(
        local_connector="discord", local_channel_id="42", local_channel_name="general",
        source="stoat", source_id="s-general", destination_id=None,
    )
    recorder = _Recorder()
    client = FakeClient(user=FakeUser(id=9, display_name="Bridge"))
    sender = _make_sender(recorder, client, linker=linker)
    guild = FakeGuild(id=123)
    parent = FakeChannel(id=42, name="general")
    author = FakeUser(id=1, display_name="isabel")
    thread = FakeThread(id=777, parent=parent, name="My New Thread", guild=guild)
    thread._starter_message = _discord_message(channel=thread, guild=guild, author=author, content="augh", id=555)

    await sender._handle_thread_create(thread)

    notice = recorder.messages[-1]
    assert notice.origin_channel_id == "42"
    assert notice.content_markdown == "isabel started a thread: <#777>"
