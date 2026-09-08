"""Shared fixtures for StoatReceiverService tests (tests/stoat_receiver/)
against the fake_stoat scaffolding (tests/fakes/fake_stoat.py).
"""

from __future__ import annotations

from types import SimpleNamespace

import aiohttp

from stoat_discord_bridge.models import StandardMessage
from stoat_discord_bridge.services.stoat_service import StoatReceiverService, StoatSenderService
from stoat_discord_bridge.storage.user_mappings import UserMappingRepository
from tests.fakes.fake_stoat import FakeClient

__all__ = ["_FakeSender", "_message", "_make_receiver", "_FakeAiohttpResponse"]


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


class _FakeSender:
    """StoatReceiverService only ever reads .connector_id and reuses the
    sender's already-connected client - stand in for StoatSenderService
    without building a real one (whose __init__ makes a real network call,
    see test_stoat_resolve_avatar.py's docstring). get_masquerade_identity is
    the real StoatSenderService implementation, bound onto this fake, since
    it's plain client-reading logic with no network-touching __init__ of its
    own to avoid."""

    def __init__(
        self,
        client: FakeClient,
        connector_id: str = "stoat",
        server_id: str = "srv-1",
        enable_local_user_masquerade: bool = True,
    ) -> None:
        self.connector_id = connector_id
        self.server_id = server_id
        self.self_id = "bridge-bot-id"
        self._client = client
        self._category_linker = None
        self._config = SimpleNamespace(
            enable_local_user_masquerade=enable_local_user_masquerade,
            group_parent_channel_with_threads=True,
        )

    def get_channel(self, channel_id: str, *, partial: bool = True):
        return self._client.get_channel(channel_id, partial=partial)

    def get_server(self, server_id: str, *, partial: bool = True):
        return self._client.get_server(server_id, partial=partial)

    get_masquerade_identity = StoatSenderService.get_masquerade_identity
    group_parent_channel_with_threads = StoatSenderService.group_parent_channel_with_threads
    _move_channel_to_category_top = StoatSenderService._move_channel_to_category_top
    _full_category_list = StoatSenderService._full_category_list


def _message(**overrides) -> StandardMessage:
    defaults = dict(
        origin_connector_id="discord",
        origin_channel_id="d-100",
        channel_name="general",
        sender_name="Alice",
        sender_avatar_url="https://cdn.example/alice.png",
        sender_user_id="discord-alice",
        content_markdown="hello",
        message_id="m1",
    )
    defaults.update(overrides)
    return StandardMessage(**defaults)


def _make_receiver(client: FakeClient, user_mappings: UserMappingRepository | None = None) -> StoatReceiverService:
    return StoatReceiverService(_FakeSender(client), user_mappings=user_mappings)

