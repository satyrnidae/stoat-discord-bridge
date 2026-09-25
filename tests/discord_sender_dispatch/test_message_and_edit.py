from __future__ import annotations

from types import SimpleNamespace

import discord

from tests.fakes.fake_discord import FakeAsset, FakeAttachment, FakeChannel, FakeClient, FakeGuild, FakeUser
from tests.discord_sender_dispatch.conftest import _Recorder, _discord_message, _make_sender


class _FakeBotWhitelist:
    """Minimal `BotWhitelistManager` stand-in - a fixed set of
    (connector_id, user_id) pairs that are whitelisted."""

    def __init__(self, *entries: tuple[str, str]) -> None:
        self._entries = set(entries)

    async def is_whitelisted(self, connector_id: str, user_id: str) -> bool:
        return (connector_id, user_id) in self._entries


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


async def test_handle_message_relays_a_whitelisted_bot_author():
    recorder = _Recorder()
    client = FakeClient()
    sender = _make_sender(recorder, client, bot_whitelist=_FakeBotWhitelist(("discord", "1")))
    guild = FakeGuild(id=123)
    channel = FakeChannel(id=42)
    author = FakeUser(id=1, bot=True)

    await sender._handle_message(_discord_message(channel=channel, guild=guild, author=author))

    assert len(recorder.messages) == 1


async def test_handle_message_still_ignores_a_non_whitelisted_bot_author():
    recorder = _Recorder()
    client = FakeClient()
    sender = _make_sender(recorder, client, bot_whitelist=_FakeBotWhitelist(("discord", "other-bot")))
    guild = FakeGuild(id=123)
    channel = FakeChannel(id=42)
    author = FakeUser(id=1, bot=True)

    await sender._handle_message(_discord_message(channel=channel, guild=guild, author=author))

    assert recorder.messages == []


async def test_handle_message_drops_a_webhook_post_even_when_the_author_is_whitelisted():
    recorder = _Recorder()
    client = FakeClient()
    sender = _make_sender(recorder, client, bot_whitelist=_FakeBotWhitelist(("discord", "1")))
    guild = FakeGuild(id=123)
    channel = FakeChannel(id=42)
    author = FakeUser(id=1, bot=True)

    await sender._handle_message(
        _discord_message(channel=channel, guild=guild, author=author, webhook_id=999)
    )

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


async def test_handle_message_carries_the_replied_to_message_id():
    recorder = _Recorder()
    sender = _make_sender(recorder, FakeClient())
    guild = FakeGuild(id=123)
    channel = FakeChannel(id=42, name="general")
    author = FakeUser(id=1, display_name="Alice")
    reference = SimpleNamespace(type=discord.MessageReferenceType.default, message_id=77)

    await sender._handle_message(
        _discord_message(channel=channel, guild=guild, author=author, content="ok", reference=reference)
    )

    [message] = recorder.messages
    assert message.reply_to_message_id == "77"


async def test_handle_message_ignores_a_forward_reference():
    recorder = _Recorder()
    sender = _make_sender(recorder, FakeClient())
    guild = FakeGuild(id=123)
    channel = FakeChannel(id=42, name="general")
    author = FakeUser(id=1, display_name="Alice")
    reference = SimpleNamespace(type=discord.MessageReferenceType.forward, message_id=77)

    await sender._handle_message(
        _discord_message(channel=channel, guild=guild, author=author, content="ok", reference=reference)
    )

    [message] = recorder.messages
    assert message.reply_to_message_id is None


async def test_handle_message_with_no_reference_has_no_reply_target():
    recorder = _Recorder()
    sender = _make_sender(recorder, FakeClient())
    guild = FakeGuild(id=123)
    channel = FakeChannel(id=42, name="general")
    author = FakeUser(id=1, display_name="Alice")

    await sender._handle_message(_discord_message(channel=channel, guild=guild, author=author, content="ok"))

    [message] = recorder.messages
    assert message.reply_to_message_id is None


# ---------------------------------------------------------------- link-preview embeds (issues #102, #164)


def _embed(*, type="gifv", url="https://tenor.com/view/cat-dance-123", video=None, image=None, thumbnail=None):
    data = {"type": type, "url": url}
    if video:
        data["video"] = {"url": video}
    if image:
        data["image"] = {"url": image}
    if thumbnail:
        data["thumbnail"] = {"url": thumbnail}
    return discord.Embed.from_dict(data)


async def test_handle_message_converts_a_gifv_embed_to_an_attachment_and_leaves_the_link():
    recorder = _Recorder()
    sender = _make_sender(recorder, FakeClient())
    guild = FakeGuild(id=123)
    channel = FakeChannel(id=42, name="general")
    author = FakeUser(id=1, display_name="Alice")
    embed = _embed(video="https://c.tenor.com/abc/tenor.mp4", image="https://c.tenor.com/abc/still.png")

    await sender._handle_message(
        _discord_message(
            channel=channel, guild=guild, author=author,
            content="https://tenor.com/view/cat-dance-123", embeds=[embed],
        )
    )

    [message] = recorder.messages
    # Stripping the link is each receiver's call now (issue #164).
    assert message.content_markdown == "https://tenor.com/view/cat-dance-123"
    [attachment] = message.attachments
    assert attachment.url == "https://c.tenor.com/abc/tenor.mp4"  # video preferred over the still image
    assert attachment.filename == "gif.mp4"
    assert attachment.content_type == "video/mp4"
    assert attachment.source_page_url == "https://tenor.com/view/cat-dance-123"


async def test_handle_message_falls_back_to_a_still_image_with_no_video():
    recorder = _Recorder()
    sender = _make_sender(recorder, FakeClient())
    guild = FakeGuild(id=123)
    channel = FakeChannel(id=42, name="general")
    author = FakeUser(id=1, display_name="Alice")
    embed = _embed(type="image", image="https://c.klipy.co/abc/still.gif")

    await sender._handle_message(
        _discord_message(
            channel=channel, guild=guild, author=author,
            content="https://klipy.co/view/abc", embeds=[embed],
        )
    )

    [message] = recorder.messages
    [attachment] = message.attachments
    assert attachment.url == "https://c.klipy.co/abc/still.gif"
    assert attachment.filename == "gif.gif"
    assert attachment.content_type == "image/gif"


async def test_handle_message_recognizes_a_klipy_host_even_with_an_unrecognized_embed_type():
    recorder = _Recorder()
    sender = _make_sender(recorder, FakeClient())
    guild = FakeGuild(id=123)
    channel = FakeChannel(id=42, name="general")
    author = FakeUser(id=1, display_name="Alice")
    embed = _embed(type="link", url="https://klipy.com/view/xyz", video="https://c.klipy.com/xyz/klipy.mp4")

    await sender._handle_message(
        _discord_message(
            channel=channel, guild=guild, author=author,
            content="https://klipy.com/view/xyz", embeds=[embed],
        )
    )

    [message] = recorder.messages
    assert len(message.attachments) == 1
    assert message.attachments[0].url == "https://c.klipy.com/xyz/klipy.mp4"


async def test_handle_message_converts_an_article_embed_with_an_image():
    recorder = _Recorder()
    sender = _make_sender(recorder, FakeClient())
    guild = FakeGuild(id=123)
    channel = FakeChannel(id=42, name="general")
    author = FakeUser(id=1, display_name="Alice")
    embed = _embed(
        type="article", url="https://example.com/some-article", image="https://cdn.example.com/hero.jpg"
    )

    await sender._handle_message(
        _discord_message(
            channel=channel, guild=guild, author=author,
            content="look https://example.com/some-article", embeds=[embed],
        )
    )

    [message] = recorder.messages
    assert message.content_markdown == "look https://example.com/some-article"
    [attachment] = message.attachments
    assert attachment.url == "https://cdn.example.com/hero.jpg"
    assert attachment.filename == "preview.jpg"
    assert attachment.content_type == "image/jpeg"
    assert attachment.source_page_url == "https://example.com/some-article"


async def test_handle_message_skips_a_video_player_url_for_the_thumbnail():
    # A YouTube-style embed's video.url is a player page, not a media file.
    recorder = _Recorder()
    sender = _make_sender(recorder, FakeClient())
    guild = FakeGuild(id=123)
    channel = FakeChannel(id=42, name="general")
    author = FakeUser(id=1, display_name="Alice")
    embed = _embed(
        type="video",
        url="https://www.youtube.com/watch?v=abc",
        video="https://www.youtube.com/embed/abc",
        thumbnail="https://i.ytimg.com/vi/abc/hqdefault.jpg",
    )

    await sender._handle_message(
        _discord_message(
            channel=channel, guild=guild, author=author,
            content="https://www.youtube.com/watch?v=abc", embeds=[embed],
        )
    )

    [message] = recorder.messages
    [attachment] = message.attachments
    assert attachment.url == "https://i.ytimg.com/vi/abc/hqdefault.jpg"


async def test_handle_message_with_a_media_less_embed_is_unaffected():
    recorder = _Recorder()
    sender = _make_sender(recorder, FakeClient())
    guild = FakeGuild(id=123)
    channel = FakeChannel(id=42, name="general")
    author = FakeUser(id=1, display_name="Alice")
    embed = _embed(type="article", url="https://example.com/some-article")

    await sender._handle_message(
        _discord_message(
            channel=channel, guild=guild, author=author,
            content="https://example.com/some-article", embeds=[embed],
        )
    )

    [message] = recorder.messages
    assert message.content_markdown == "https://example.com/some-article"
    assert message.attachments == []


async def test_handle_message_with_no_embeds_is_unaffected():
    recorder = _Recorder()
    sender = _make_sender(recorder, FakeClient())
    guild = FakeGuild(id=123)
    channel = FakeChannel(id=42, name="general")
    author = FakeUser(id=1, display_name="Alice")

    await sender._handle_message(
        _discord_message(channel=channel, guild=guild, author=author, content="just text")
    )

    [message] = recorder.messages
    assert message.content_markdown == "just text"
    assert message.attachments == []


# ---------------------------------------------------------------- Discord forwarded messages (issue #125)


class _FakeMessageSnapshot:
    def __init__(self, content="", attachments=None):
        self.content = content
        self.attachments = attachments or []


async def test_handle_message_relays_a_forward_with_no_caption():
    # issue #125: a forward with no caption has an empty message.content -
    # the forwarded text lives only in message_snapshots, and must not be
    # dropped.
    recorder = _Recorder()
    sender = _make_sender(recorder, FakeClient())
    guild = FakeGuild(id=123)
    channel = FakeChannel(id=42, name="general")
    author = FakeUser(id=1, display_name="Alice")

    await sender._handle_message(
        _discord_message(
            channel=channel, guild=guild, author=author, content="", id=99,
            message_snapshots=[_FakeMessageSnapshot(content="the original message")],
        )
    )

    [message] = recorder.messages
    assert message.content_markdown == "> the original message"


async def test_handle_message_relays_a_forward_with_a_caption():
    # issue #125: a caption alongside a forward must not eclipse the
    # forwarded content - both need to reach the destination.
    recorder = _Recorder()
    sender = _make_sender(recorder, FakeClient())
    guild = FakeGuild(id=123)
    channel = FakeChannel(id=42, name="general")
    author = FakeUser(id=1, display_name="Alice")

    await sender._handle_message(
        _discord_message(
            channel=channel, guild=guild, author=author, content="check this out", id=99,
            message_snapshots=[_FakeMessageSnapshot(content="the original message")],
        )
    )

    [message] = recorder.messages
    assert message.content_markdown == "check this out\n\n> the original message"


async def test_handle_message_forward_quotes_every_line_of_multiline_content():
    recorder = _Recorder()
    sender = _make_sender(recorder, FakeClient())
    guild = FakeGuild(id=123)
    channel = FakeChannel(id=42, name="general")
    author = FakeUser(id=1, display_name="Alice")

    await sender._handle_message(
        _discord_message(
            channel=channel, guild=guild, author=author, content="", id=99,
            message_snapshots=[_FakeMessageSnapshot(content="line one\n\nline three")],
        )
    )

    [message] = recorder.messages
    assert message.content_markdown == "> line one\n>\n> line three"


async def test_handle_message_forward_carries_over_the_snapshots_attachments():
    recorder = _Recorder()
    sender = _make_sender(recorder, FakeClient())
    guild = FakeGuild(id=123)
    channel = FakeChannel(id=42, name="general")
    author = FakeUser(id=1, display_name="Alice")
    own_attachment = FakeAttachment(url="https://cdn.example/caption.png", filename="caption.png")
    snapshot_attachment = FakeAttachment(url="https://cdn.example/forwarded.png", filename="forwarded.png")

    await sender._handle_message(
        _discord_message(
            channel=channel, guild=guild, author=author, content="", id=99, attachments=[own_attachment],
            message_snapshots=[_FakeMessageSnapshot(content="a photo", attachments=[snapshot_attachment])],
        )
    )

    [message] = recorder.messages
    assert [a.url for a in message.attachments] == [
        "https://cdn.example/caption.png",
        "https://cdn.example/forwarded.png",
    ]


async def test_handle_message_non_forward_is_unaffected_by_empty_snapshots():
    recorder = _Recorder()
    sender = _make_sender(recorder, FakeClient())
    guild = FakeGuild(id=123)
    channel = FakeChannel(id=42, name="general")
    author = FakeUser(id=1, display_name="Alice")

    await sender._handle_message(
        _discord_message(channel=channel, guild=guild, author=author, content="just a normal message", id=99)
    )

    [message] = recorder.messages
    assert message.content_markdown == "just a normal message"


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


async def test_handle_raw_message_edit_ignores_a_non_whitelisted_bot_authored_edit():
    recorder = _Recorder()
    sender = _make_sender(recorder, FakeClient())

    await sender._handle_raw_message_edit(
        _edit_payload(
            data={
                "content": "x",
                "edited_timestamp": "2026-09-03T00:00:00+00:00",
                "author": {"id": "5", "bot": True},
            }
        )
    )

    assert recorder.edits == []


async def test_handle_raw_message_edit_relays_a_whitelisted_bot_authored_edit():
    recorder = _Recorder()
    sender = _make_sender(recorder, FakeClient(), bot_whitelist=_FakeBotWhitelist(("discord", "5")))

    await sender._handle_raw_message_edit(
        _edit_payload(
            data={
                "content": "fixed typo",
                "edited_timestamp": "2026-09-03T00:00:00+00:00",
                "author": {"id": "5", "bot": True},
            }
        )
    )

    assert [e.new_content_markdown for e in recorder.edits] == ["fixed typo"]


# -------------------------------------------------------- _handle_raw_message_delete


def _delete_payload(**overrides):
    defaults = dict(guild_id=123, channel_id=42, message_id=7, cached_message=None)
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _bulk_delete_payload(**overrides):
    defaults = dict(guild_id=123, channel_id=42, message_ids={7, 8}, cached_messages=[])
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


async def test_handle_raw_message_delete_emits_a_standard_delete():
    recorder = _Recorder()
    sender = _make_sender(recorder, FakeClient())

    await sender._handle_raw_message_delete(_delete_payload())

    assert [(d.origin_connector_id, d.origin_channel_id, d.origin_message_id) for d in recorder.deletes] == [
        ("discord", "42", "7")
    ]


async def test_handle_raw_message_delete_ignores_a_different_guild():
    recorder = _Recorder()
    sender = _make_sender(recorder, FakeClient())

    await sender._handle_raw_message_delete(_delete_payload(guild_id=999))

    assert recorder.deletes == []


async def test_handle_raw_message_delete_drops_a_cached_webhook_copy():
    recorder = _Recorder()
    sender = _make_sender(recorder, FakeClient())

    await sender._handle_raw_message_delete(
        _delete_payload(cached_message=SimpleNamespace(webhook_id=999))
    )

    assert recorder.deletes == []


async def test_handle_raw_message_delete_relays_when_cached_message_has_no_webhook_id():
    recorder = _Recorder()
    sender = _make_sender(recorder, FakeClient())

    await sender._handle_raw_message_delete(
        _delete_payload(cached_message=SimpleNamespace(webhook_id=None))
    )

    assert len(recorder.deletes) == 1


async def test_handle_raw_bulk_message_delete_emits_one_standard_delete_per_id():
    recorder = _Recorder()
    sender = _make_sender(recorder, FakeClient())

    await sender._handle_raw_bulk_message_delete(_bulk_delete_payload())

    assert {(d.origin_channel_id, d.origin_message_id) for d in recorder.deletes} == {("42", "7"), ("42", "8")}


async def test_handle_raw_bulk_message_delete_ignores_a_different_guild():
    recorder = _Recorder()
    sender = _make_sender(recorder, FakeClient())

    await sender._handle_raw_bulk_message_delete(_bulk_delete_payload(guild_id=999))

    assert recorder.deletes == []


async def test_handle_raw_bulk_message_delete_drops_cached_webhook_copies_individually():
    recorder = _Recorder()
    sender = _make_sender(recorder, FakeClient())

    await sender._handle_raw_bulk_message_delete(
        _bulk_delete_payload(
            cached_messages=[SimpleNamespace(id=7, webhook_id=999), SimpleNamespace(id=8, webhook_id=None)]
        )
    )

    assert [(d.origin_channel_id, d.origin_message_id) for d in recorder.deletes] == [("42", "8")]


