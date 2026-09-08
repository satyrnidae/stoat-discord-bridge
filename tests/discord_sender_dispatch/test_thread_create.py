from __future__ import annotations

import discord

from stoat_discord_bridge.admin_commands import ChannelLinker, ConnectorInfo
from stoat_discord_bridge.storage.channel_mappings import ChannelMappingRepository
from tests.fakes.fake_discord import FakeAsset, FakeChannel, FakeClient, FakeGuild, FakeThread, FakeUser
from tests.discord_sender_dispatch.conftest import _Recorder, _discord_message, _make_sender


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
