"""Shared fixtures for DiscordSenderService's gateway-event handler tests
(tests/discord_sender_dispatch/) against the fake_discord scaffolding.
Builds a real DiscordSenderService (safe: its constructor does no network
I/O, same as test_discord_service.py) and swaps its `_client` for a
FakeClient so these handlers read fake gateway state instead of a live
discord.py cache.
"""

from __future__ import annotations

from types import SimpleNamespace

import discord

from stoat_discord_bridge.config import DiscordConnectorConfig
from stoat_discord_bridge.services.discord_service import DiscordSenderService
from stoat_discord_bridge.status import HealthTracker
from tests.fakes.fake_discord import FakeClient

__all__ = ["_discord_config", "_Recorder", "_make_sender", "_discord_message"]


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
    mentions=None, role_mentions=None, channel_mentions=None,
):
    return SimpleNamespace(
        channel=channel, guild=guild, author=author, content=content, id=id, attachments=attachments or [],
        type=type, thread=thread, mentions=mentions or [], role_mentions=role_mentions or [],
        channel_mentions=channel_mentions or [],
    )
