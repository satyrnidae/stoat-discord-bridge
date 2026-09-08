"""Shared fixtures for DiscordReceiverService tests (tests/discord_receiver/)
against the fake_discord scaffolding (tests/fakes/fake_discord.py).
"""

from __future__ import annotations

import aiohttp

from stoat_discord_bridge.models import StandardEdit, StandardMessage
from stoat_discord_bridge.services.discord_service import DiscordReceiverService
from stoat_discord_bridge.storage.user_mappings import UserMappingRepository
from tests.fakes.fake_discord import FakeClient

__all__ = ["_message", "_edit", "_make_receiver", "_FakeAiohttpResponse"]


class _FakeAiohttpResponse:
    def __init__(self, body: bytes, *, status: int = 200) -> None:
        self._body = body
        self.status = status

    async def __aenter__(self) -> "_FakeAiohttpResponse":
        return self

    async def __aexit__(self, *exc_info) -> bool:
        return False

    def raise_for_status(self) -> None:
        if self.status >= 400:
            raise aiohttp.ClientResponseError(None, (), status=self.status)

    async def read(self) -> bytes:
        return self._body


def _edit(**overrides) -> StandardEdit:
    defaults = dict(
        origin_connector_id="stoat",
        origin_channel_id="s-100",
        origin_message_id="m1",
        new_content_markdown="edited text",
    )
    defaults.update(overrides)
    return StandardEdit(**defaults)


def _message(**overrides) -> StandardMessage:
    defaults = dict(
        origin_connector_id="stoat",
        origin_channel_id="s-100",
        channel_name="general",
        sender_name="Alice",
        sender_avatar_url="https://cdn.example/alice.png",
        sender_user_id="stoat-alice",
        content_markdown="hello",
        message_id="m1",
    )
    defaults.update(overrides)
    return StandardMessage(**defaults)


def _make_receiver(client: FakeClient, user_mappings: UserMappingRepository | None = None) -> DiscordReceiverService:
    return DiscordReceiverService(client, guild_id=123, connector_id="discord", user_mappings=user_mappings)

