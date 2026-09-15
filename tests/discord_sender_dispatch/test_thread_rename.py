from __future__ import annotations

import discord

from tests.discord_sender_dispatch.conftest import _Recorder, _discord_message, _make_sender
from tests.fakes.fake_discord import FakeChannel, FakeClient, FakeForumChannel, FakeGuild, FakeThread, FakeUser


# ---------------------------------------------------------------- channel_name_change suppression + notice


async def test_channel_name_change_in_a_plain_thread_is_suppressed_and_relayed_as_a_notice():
    recorder = _Recorder()
    sender = _make_sender(recorder, FakeClient())
    guild = FakeGuild(id=123)
    parent = FakeChannel(id=1, name="general")
    thread = FakeThread(id=99, parent=parent, name="renamed-thread", guild=guild)
    author = FakeUser(id=7, display_name="Alice")
    message = _discord_message(
        channel=thread, guild=guild, author=author, content="new-name",
        type=discord.MessageType.channel_name_change, id=555,
    )

    await sender._handle_message(message)

    [notice] = recorder.messages
    assert notice.origin_connector_id == "discord"
    assert notice.origin_channel_id == "99"
    assert notice.content_markdown == "<@7> changed the channel name: new-name"
    assert notice.mentioned_users == {"7": "Alice"}
    assert notice.message_id == "thread-renamed:555"


async def test_channel_name_change_in_a_forum_post_uses_post_title_wording():
    recorder = _Recorder()
    sender = _make_sender(recorder, FakeClient())
    guild = FakeGuild(id=123)
    forum = FakeForumChannel(id=1, name="announcements")
    thread = FakeThread(id=99, parent=forum, name="renamed-post", guild=guild)
    author = FakeUser(id=7, display_name="Alice")
    message = _discord_message(
        channel=thread, guild=guild, author=author, content="Xeno Metaseries",
        type=discord.MessageType.channel_name_change, id=556,
    )

    await sender._handle_message(message)

    [notice] = recorder.messages
    assert notice.content_markdown == "<@7> changed the post title: Xeno Metaseries"


async def test_channel_name_change_notifies_the_channel_renamed_callback():
    recorder = _Recorder()
    sender = _make_sender(recorder, FakeClient())
    guild = FakeGuild(id=123)
    parent = FakeChannel(id=1, name="general")
    thread = FakeThread(id=99, parent=parent, name="renamed-thread", guild=guild)
    author = FakeUser(id=7, display_name="Alice")
    message = _discord_message(
        channel=thread, guild=guild, author=author, content="new-name",
        type=discord.MessageType.channel_name_change, id=555,
    )

    await sender._handle_message(message)

    assert recorder.channel_renames == [("discord", "99", "new-name")]


async def test_channel_name_change_is_a_noop_without_the_callback_wired():
    recorder = _Recorder()
    sender = _make_sender(recorder, FakeClient())
    sender._on_channel_renamed = None
    guild = FakeGuild(id=123)
    parent = FakeChannel(id=1, name="general")
    thread = FakeThread(id=99, parent=parent, name="renamed-thread", guild=guild)
    author = FakeUser(id=7, display_name="Alice")
    message = _discord_message(
        channel=thread, guild=guild, author=author, content="new-name",
        type=discord.MessageType.channel_name_change, id=555,
    )

    await sender._handle_message(message)  # must not raise

    [notice] = recorder.messages
    assert notice.content_markdown == "<@7> changed the channel name: new-name"


async def test_channel_name_change_defangs_a_mass_ping_in_the_new_name():
    # The new name is user-supplied text spliced into relayed content -
    # a thread renamed to "@everyone" shouldn't relay a live mass ping
    # (the bridge sets no allowed_mentions on its webhook sends).
    recorder = _Recorder()
    sender = _make_sender(recorder, FakeClient())
    guild = FakeGuild(id=123)
    parent = FakeChannel(id=1, name="general")
    thread = FakeThread(id=99, parent=parent, name="renamed-thread", guild=guild)
    author = FakeUser(id=7, display_name="Alice")
    message = _discord_message(
        channel=thread, guild=guild, author=author, content="@everyone",
        type=discord.MessageType.channel_name_change, id=555,
    )

    await sender._handle_message(message)

    [notice] = recorder.messages
    assert "@everyone" not in notice.content_markdown
    assert "everyone" in notice.content_markdown  # defanged, not dropped entirely
    # The propagated rename itself still uses the real, un-defanged name -
    # it's a channel name on the target, not rendered as chat content.
    assert recorder.channel_renames == [("discord", "99", "@everyone")]


async def test_channel_name_change_outside_a_thread_is_not_suppressed():
    # Discord doesn't emit this message type for an ordinary guild channel in
    # practice, but the suppression is scoped to discord.Thread defensively -
    # anything else falls through to the normal relay path unaffected.
    recorder = _Recorder()
    sender = _make_sender(recorder, FakeClient())
    guild = FakeGuild(id=123)
    channel = FakeChannel(id=1, name="general")
    author = FakeUser(id=7, display_name="Alice")
    message = _discord_message(
        channel=channel, guild=guild, author=author, content="new-name",
        type=discord.MessageType.channel_name_change, id=555,
    )

    await sender._handle_message(message)

    [relayed] = recorder.messages
    assert relayed.message_id == "555"
    assert recorder.channel_renames == []
