from __future__ import annotations

from types import SimpleNamespace

import discord

from tests.fakes.fake_discord import FakeAsset, FakeAttachment, FakeChannel, FakeClient, FakeGuild, FakeUser
from tests.discord_sender_dispatch.conftest import _Recorder, _discord_message, _make_sender


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


async def test_handle_message_maps_role_mentions():
    recorder = _Recorder()
    sender = _make_sender(recorder, FakeClient())
    guild = FakeGuild(id=123)
    channel = FakeChannel(id=42, name="general")
    author = FakeUser(id=1, display_name="Alice")

    await sender._handle_message(
        _discord_message(
            channel=channel, guild=guild, author=author, content="ping <@&7>",
            role_mentions=[SimpleNamespace(id=7, name="Mods")],
        )
    )

    [message] = recorder.messages
    assert message.mentioned_roles == {"7": "Mods"}


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
            message=SimpleNamespace(
                mentions=[SimpleNamespace(id=9, display_name="Bob")],
                role_mentions=[SimpleNamespace(id=3, name="Mods")],
                channel_mentions=[SimpleNamespace(id=4, name="off-topic")],
            ),
        )
    )

    assert [
        (
            e.origin_channel_id,
            e.origin_message_id,
            e.new_content_markdown,
            e.mentioned_users,
            e.mentioned_roles,
            e.mentioned_channels,
        )
        for e in recorder.edits
    ] == [("42", "7", "fixed typo", {"9": "Bob"}, {"3": "Mods"}, {"4": "off-topic"})]


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


