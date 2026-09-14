"""Tests for DiscordSenderService.fetch_history - the Discord half of the
`ConnectorInfo.fetch_history` hook behind `/mirror channel with history`
(issue #122). Converts channel.history() (a real gateway/REST call in
discord.py) into StandardMessages via the same _to_standard_message the live
relay path uses, skipping webhook echoes, non-whitelisted bots, and system
messages the way _handle_message already does.
"""

from __future__ import annotations

import discord

from tests.discord_sender_dispatch.conftest import _Recorder, _discord_message, _make_sender
from tests.fakes.fake_discord import FakeChannel, FakeClient, FakeGuild, FakeUser


class _FakeBotWhitelist:
    def __init__(self, *entries: tuple[str, str]) -> None:
        self._entries = set(entries)

    async def is_whitelisted(self, connector_id: str, user_id: str) -> bool:
        return (connector_id, user_id) in self._entries


async def test_fetch_history_converts_messages_oldest_first():
    client = FakeClient()
    guild = FakeGuild(id=123)
    channel = FakeChannel(id=42, guild=guild)
    client.add_channel(channel)
    author = FakeUser(id=1, display_name="Alice")
    channel.set_history(
        [
            _discord_message(channel=channel, guild=guild, author=author, content="first", id=1),
            _discord_message(channel=channel, guild=guild, author=author, content="second", id=2),
        ]
    )
    sender = _make_sender(recorder=_Recorder(), client=client)

    messages = await sender.fetch_history("42", None)

    assert [m.content_markdown for m in messages] == ["first", "second"]
    assert messages[0].origin_connector_id == "discord"
    assert messages[0].message_id == "1"


async def test_fetch_history_respects_a_numeric_limit():
    # A numeric limit means the N *most recent* messages (issue #122), not
    # the oldest N - still returned oldest-first so relay order is correct.
    client = FakeClient()
    guild = FakeGuild(id=123)
    channel = FakeChannel(id=42, guild=guild)
    client.add_channel(channel)
    author = FakeUser(id=1, display_name="Alice")
    channel.set_history(
        [
            _discord_message(channel=channel, guild=guild, author=author, content=f"m{i}", id=i)
            for i in range(1, 6)
        ]
    )
    sender = _make_sender(recorder=_Recorder(), client=client)

    messages = await sender.fetch_history("42", 2)

    assert [m.content_markdown for m in messages] == ["m4", "m5"]


async def test_fetch_history_drops_webhook_posts():
    client = FakeClient()
    guild = FakeGuild(id=123)
    channel = FakeChannel(id=42, guild=guild)
    client.add_channel(channel)
    author = FakeUser(id=1, display_name="Bridge")
    channel.set_history(
        [_discord_message(channel=channel, guild=guild, author=author, content="relayed", id=1, webhook_id=999)]
    )
    sender = _make_sender(recorder=_Recorder(), client=client)

    messages = await sender.fetch_history("42", None)

    assert messages == []


async def test_fetch_history_drops_a_non_whitelisted_bot_author():
    client = FakeClient()
    guild = FakeGuild(id=123)
    channel = FakeChannel(id=42, guild=guild)
    client.add_channel(channel)
    author = FakeUser(id=1, bot=True)
    channel.set_history([_discord_message(channel=channel, guild=guild, author=author, content="beep", id=1)])
    sender = _make_sender(recorder=_Recorder(), client=client)

    messages = await sender.fetch_history("42", None)

    assert messages == []


async def test_fetch_history_relays_a_whitelisted_bot_author():
    client = FakeClient()
    guild = FakeGuild(id=123)
    channel = FakeChannel(id=42, guild=guild)
    client.add_channel(channel)
    author = FakeUser(id=1, bot=True)
    channel.set_history([_discord_message(channel=channel, guild=guild, author=author, content="beep", id=1)])
    sender = _make_sender(recorder=_Recorder(), client=client, bot_whitelist=_FakeBotWhitelist(("discord", "1")))

    messages = await sender.fetch_history("42", None)

    assert [m.content_markdown for m in messages] == ["beep"]


async def test_fetch_history_drops_system_messages():
    client = FakeClient()
    guild = FakeGuild(id=123)
    channel = FakeChannel(id=42, guild=guild)
    client.add_channel(channel)
    author = FakeUser(id=1, display_name="Alice")
    channel.set_history(
        [
            _discord_message(
                channel=channel, guild=guild, author=author, content="", id=1,
                type=discord.MessageType.pins_add,
            ),
            _discord_message(channel=channel, guild=guild, author=author, content="real one", id=2),
        ]
    )
    sender = _make_sender(recorder=_Recorder(), client=client)

    messages = await sender.fetch_history("42", None)

    assert [m.content_markdown for m in messages] == ["real one"]


async def test_fetch_history_returns_empty_for_a_channel_outside_the_configured_guild():
    client = FakeClient()
    other_guild = FakeGuild(id=999)  # not this sender's configured guild_id (123)
    channel = FakeChannel(id=42, guild=other_guild)
    client.add_channel(channel)
    author = FakeUser(id=1, display_name="Alice")
    channel.set_history([_discord_message(channel=channel, guild=other_guild, author=author, content="hi", id=1)])
    sender = _make_sender(recorder=_Recorder(), client=client)

    messages = await sender.fetch_history("42", None)

    assert messages == []


async def test_fetch_history_returns_empty_for_an_unresolvable_channel():
    client = FakeClient()
    sender = _make_sender(recorder=_Recorder(), client=client)

    messages = await sender.fetch_history("999999", None)

    assert messages == []
