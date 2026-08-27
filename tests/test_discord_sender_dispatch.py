"""Tests for DiscordSenderService's gateway-event handlers - _handle_message,
_handle_raw_reaction/_is_other_bot, and _handle_guild_emojis_update - against
the fake_discord scaffolding. Builds a real DiscordSenderService (safe: its
constructor does no network I/O, same as test_discord_service.py) and swaps
its `_client` for a FakeClient so these handlers read fake gateway state
instead of a live discord.py cache.
"""

from __future__ import annotations

from types import SimpleNamespace

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

    async def on_message(self, message) -> None:
        self.messages.append(message)

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
        linker=linker,
        category_linker=category_linker,
    )
    sender._client = client
    return sender


def _discord_message(*, channel, guild, author, content="hi", id=1, attachments=None):
    return SimpleNamespace(channel=channel, guild=guild, author=author, content=content, id=id, attachments=attachments or [])


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
    assert [a.url for a in message.attachments] == ["https://cdn.example/f.png"]


async def test_handle_message_uses_none_avatar_when_the_author_has_no_avatar():
    recorder = _Recorder()
    client = FakeClient()
    sender = _make_sender(recorder, client)
    guild = FakeGuild(id=123)
    channel = FakeChannel(id=42)
    author = FakeUser(id=1, display_avatar=None)

    await sender._handle_message(_discord_message(channel=channel, guild=guild, author=author))

    assert recorder.messages[0].sender_avatar_url is None


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


# ---------------------------------------------------------------- _handle_thread_create


async def _stoat_ensure_channel(name: str, category: str | None = None, is_thread_category: bool = False) -> str:
    return f"stoat_{name}"


async def _irc_ensure_channel(name: str, category: str | None = None, is_thread_category: bool = False) -> str:
    return f"irc_{name}"


def _linked_connectors():
    return {
        "discord": ConnectorInfo(id="discord", label="Discord"),
        "stoat": ConnectorInfo(id="stoat", label="Stoat", ensure_channel=_stoat_ensure_channel),
        "irc": ConnectorInfo(id="irc", label="IRC", ensure_channel=_irc_ensure_channel),
    }


async def test_handle_thread_create_mirrors_and_announces_when_parent_is_bridged(fake_db):
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
    thread = FakeThread(id=777, parent=parent, name="Test Thread", guild=FakeGuild(id=123))

    await sender._handle_thread_create(thread)

    group = await channel_mappings.get_bridge_group("discord", "777")
    assert group is not None
    mapped = {m.connector_id: m.channel_id for m in await channel_mappings.get_mapped_channels(group)}
    assert mapped["stoat"] == "stoat_Test Thread"
    assert mapped["irc"] == "irc_Test Thread"

    [message] = recorder.messages
    assert message.origin_connector_id == "discord"
    assert message.origin_channel_id == "777"
    assert message.content_markdown == "Created a new channel https://discord.com/channels/123/777"
    assert message.sender_name == "Bridge"
    assert message.message_id == "thread-created-777"


async def test_handle_thread_create_uses_parent_channel_name_as_category(fake_db):
    calls = []

    async def stoat_ensure_channel(name, category=None, is_thread_category=False):
        calls.append((name, category))
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

    assert calls == [("Test Thread", "Announcements")]  # category = parent's name, not any real Discord Category


async def test_handle_thread_create_marks_destination_category_as_thread_category(fake_db):
    calls = []

    async def stoat_ensure_channel(name, category=None, is_thread_category=False):
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


async def test_handle_message_suppresses_the_threads_starter_message(fake_db):
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

    await sender._handle_thread_create(thread)
    await sender._handle_message(_discord_message(channel=thread, guild=guild, author=author, content="Test Thread"))

    # only the "Created a new channel" announcement made it through - the
    # thread's own starter message (which duplicates the thread name) was
    # swallowed, not relayed a second time.
    assert len(recorder.messages) == 1
    assert recorder.messages[0].content_markdown.startswith("Created a new channel")

    # a later, ordinary message in the same thread relays normally - the
    # suppression is one-shot.
    await sender._handle_message(_discord_message(channel=thread, guild=guild, author=author, content="follow-up"))
    assert len(recorder.messages) == 2
    assert recorder.messages[1].content_markdown == "follow-up"
