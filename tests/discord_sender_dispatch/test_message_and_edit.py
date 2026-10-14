from __future__ import annotations

import logging
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
    assert message.attachments[0].description is None


async def test_handle_message_carries_an_attachments_alt_text():
    recorder = _Recorder()
    sender = _make_sender(recorder, FakeClient())
    attachment = FakeAttachment(
        url="https://cdn.example/cat.png", filename="cat.png", content_type="image/png", size=10,
        description="a cat asleep on a keyboard",
    )

    await sender._handle_message(
        _discord_message(
            channel=FakeChannel(id=42), guild=FakeGuild(id=123), author=FakeUser(id=1), attachments=[attachment]
        )
    )

    [message] = recorder.messages
    assert message.attachments[0].description == "a cat asleep on a keyboard"


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


# An uncached edit only counts when its timestamp is recent, so fixtures use the real clock.
_JUST_NOW = discord.utils.utcnow().isoformat()
_LONG_AGO = "2026-01-01T00:00:00+00:00"


def _edit_payload(**overrides):
    defaults = dict(guild_id=123, channel_id=42, message_id=7, data={})
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


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
                "edited_timestamp": _JUST_NOW,
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
            data={"content": "x", "edited_timestamp": _JUST_NOW, "webhook_id": "123"}
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
                "edited_timestamp": _JUST_NOW,
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
                "edited_timestamp": _JUST_NOW,
                "author": {"id": "5", "bot": True},
            }
        )
    )

    assert [e.new_content_markdown for e in recorder.edits] == ["fixed typo"]



# Discord sends the whole message on every MESSAGE_UPDATE (discord.py 2.5+
# builds a full Message from it), so `pinned`, `content` and
# `edited_timestamp` are on every payload (issue #227).


def _full_payload(*, pinned=False, edited_timestamp=None, content="fixed typo", cached=None, embeds=None):
    return _edit_payload(
        data={
            "content": content,
            "pinned": pinned,
            "edited_timestamp": edited_timestamp,
            "author": {"id": "1", "bot": False},
            "embeds": [],
        },
        message=SimpleNamespace(
            content=content, embeds=embeds or [], mentions=[], role_mentions=[], channel_mentions=[]
        ),
        cached_message=cached,
    )


def _cached(*, pinned=False, edited_timestamp=None):
    return SimpleNamespace(pinned=pinned, edited_at=discord.utils.parse_time(edited_timestamp))


async def test_handle_raw_message_edit_full_payload_edit_of_an_uncached_message_is_an_edit():
    recorder = _Recorder()
    sender = _make_sender(recorder, FakeClient())

    await sender._handle_raw_message_edit(_full_payload(edited_timestamp=_JUST_NOW))

    assert [e.new_content_markdown for e in recorder.edits] == ["fixed typo"]


async def test_handle_raw_message_edit_old_edited_timestamp_on_an_uncached_message_is_not_an_edit():
    # A pin or unfurl on a message edited long ago, which isn't cached.
    recorder = _Recorder()
    sender = _make_sender(recorder, FakeClient())

    await sender._handle_raw_message_edit(_full_payload(pinned=True, edited_timestamp=_LONG_AGO))

    assert [p.pinned for p in recorder.pins] == [True]
    assert recorder.edits == []


async def test_handle_raw_message_edit_full_payload_edit_of_a_cached_message_is_only_an_edit():
    recorder = _Recorder()
    sender = _make_sender(recorder, FakeClient())

    await sender._handle_raw_message_edit(
        _full_payload(edited_timestamp=_JUST_NOW, cached=_cached(edited_timestamp=_LONG_AGO))
    )

    assert [e.new_content_markdown for e in recorder.edits] == ["fixed typo"]
    assert recorder.pins == []


async def test_handle_raw_message_edit_full_payload_pin_of_a_cached_message_is_only_a_pin():
    recorder = _Recorder()
    sender = _make_sender(recorder, FakeClient())

    await sender._handle_raw_message_edit(_full_payload(pinned=True, cached=_cached()))

    assert [p.pinned for p in recorder.pins] == [True]
    assert recorder.edits == []


async def test_handle_raw_message_edit_pin_of_an_already_edited_message_is_not_an_edit():
    recorder = _Recorder()
    sender = _make_sender(recorder, FakeClient())

    await sender._handle_raw_message_edit(
        _full_payload(pinned=True, edited_timestamp=_JUST_NOW, cached=_cached(edited_timestamp=_JUST_NOW))
    )

    assert [p.pinned for p in recorder.pins] == [True]
    assert recorder.edits == []


async def test_handle_raw_message_edit_full_payload_pin_of_an_uncached_message_is_a_pin():
    recorder = _Recorder()
    sender = _make_sender(recorder, FakeClient())

    await sender._handle_raw_message_edit(_full_payload(pinned=True))

    assert [p.pinned for p in recorder.pins] == [True]
    assert recorder.edits == []


async def test_handle_raw_message_edit_logs_the_payload_keys_at_debug(caplog):
    recorder = _Recorder()
    sender = _make_sender(recorder, FakeClient())

    with caplog.at_level(logging.DEBUG, logger="stoat_discord_bridge.services.discord_service.sender"):
        await sender._handle_raw_message_edit(_full_payload(pinned=True, cached=_cached()))

    assert any(
        "author, content, edited_timestamp, embeds, pinned" in record.getMessage()
        for record in caplog.records
        if record.levelno == logging.DEBUG
    )


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
