"""Tests for StoatReceiverService against the fake_stoat scaffolding
(tests/fakes/fake_stoat.py) - receive()'s masquerade posting/chunking/
partial-failure behavior, reaction add/remove, and custom emoji mirroring
(aiohttp's image download is monkeypatched, not real).
"""

from __future__ import annotations

import aiohttp
import pytest

from stoat_discord_bridge.models import CustomEmoji, StandardMessage
from stoat_discord_bridge.services.base import PartialRelayError
from stoat_discord_bridge.services.stoat_service import StoatReceiverService
from tests.fakes.fake_stoat import FakeChannel, FakeClient, FakeServer


class _FakeSender:
    """StoatReceiverService only ever reads .connector_id and reuses the
    sender's already-connected client - stand in for StoatSenderService
    without building a real one (whose __init__ makes a real network call,
    see test_stoat_resolve_avatar.py's docstring)."""

    def __init__(self, client: FakeClient, connector_id: str = "stoat", server_id: str = "srv-1") -> None:
        self.connector_id = connector_id
        self.server_id = server_id
        self._client = client

    def get_channel(self, channel_id: str, *, partial: bool = True):
        return self._client.get_channel(channel_id, partial=partial)

    def get_server(self, server_id: str, *, partial: bool = True):
        return self._client.get_server(server_id, partial=partial)


def _message(**overrides) -> StandardMessage:
    defaults = dict(
        origin_connector_id="discord",
        origin_channel_id="d-100",
        channel_name="general",
        sender_name="Alice",
        sender_avatar_url="https://cdn.example/alice.png",
        content_markdown="hello",
        message_id="m1",
    )
    defaults.update(overrides)
    return StandardMessage(**defaults)


def _make_receiver(client: FakeClient) -> StoatReceiverService:
    return StoatReceiverService(_FakeSender(client))


# ---------------------------------------------------------------- receive()


async def test_receive_posts_through_masquerade():
    client = FakeClient()
    channel = client.add_channel(FakeChannel(id="42"))
    receiver = _make_receiver(client)

    ids = await receiver.receive(_message(), target_channel_id="42")

    assert channel.sent == [{"content": "hello", "masquerade": channel.sent[0]["masquerade"]}]
    masquerade = channel.sent[0]["masquerade"]
    assert masquerade.name == "Alice"
    assert masquerade.avatar == "https://cdn.example/alice.png"
    assert ids == ["1"]


async def test_receive_truncates_a_sender_name_over_32_chars():
    client = FakeClient()
    channel = client.add_channel(FakeChannel(id="42"))
    receiver = _make_receiver(client)

    await receiver.receive(_message(sender_name="x" * 40), target_channel_id="42")

    assert channel.sent[0]["masquerade"].name == "x" * 32


async def test_receive_splits_long_content_into_multiple_sends(monkeypatch):
    client = FakeClient()
    channel = client.add_channel(FakeChannel(id="42"))
    receiver = _make_receiver(client)
    monkeypatch.setattr("stoat_discord_bridge.services.stoat_service._CONTENT_LIMIT", 5)

    ids = await receiver.receive(_message(content_markdown="abcdefghij"), target_channel_id="42")

    assert [call["content"] for call in channel.sent] == ["abcde", "fghij"]
    assert ids == ["1", "2"]


async def test_receive_raises_partial_relay_error_and_keeps_ids_already_sent(monkeypatch):
    client = FakeClient()
    channel = client.add_channel(FakeChannel(id="42"))
    receiver = _make_receiver(client)
    monkeypatch.setattr("stoat_discord_bridge.services.stoat_service._CONTENT_LIMIT", 5)

    # first chunk should succeed before the second fails.
    real_send = channel.send
    call_count = 0

    async def flaky_send(content, *, masquerade=None):
        nonlocal call_count
        call_count += 1
        if call_count == 2:
            raise RuntimeError("rate limited")
        return await real_send(content, masquerade=masquerade)

    channel.send = flaky_send

    with pytest.raises(PartialRelayError) as exc_info:
        await receiver.receive(_message(content_markdown="abcdefghij"), target_channel_id="42")

    assert exc_info.value.partial_ids == ["1"]


# ---------------------------------------------------------------- reactions


async def test_add_reaction_targets_the_right_message():
    client = FakeClient()
    channel = client.add_channel(FakeChannel(id="42"))
    receiver = _make_receiver(client)

    await receiver.add_reaction(target_channel_id="42", target_message_id="7", emoji="\U0001f600")

    assert channel.get_message("7").added_reactions == ["\U0001f600"]


async def test_remove_reaction_targets_the_right_message():
    client = FakeClient()
    channel = client.add_channel(FakeChannel(id="42"))
    receiver = _make_receiver(client)

    await receiver.remove_reaction(target_channel_id="42", target_message_id="7", emoji="\U0001f600")

    assert channel.get_message("7").removed_reactions == ["\U0001f600"]


async def test_add_reaction_translates_a_custom_emoji_to_its_native_id():
    client = FakeClient()
    channel = client.add_channel(FakeChannel(id="42"))
    receiver = _make_receiver(client)

    await receiver.add_reaction(
        target_channel_id="42",
        target_message_id="7",
        emoji=CustomEmoji(native_id="stoat-555", name="smile", image_url="https://cdn.example/e.png"),
    )

    assert channel.get_message("7").added_reactions == ["stoat-555"]


# ---------------------------------------------------------------- create_emoji


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


async def test_create_emoji_downloads_and_mirrors_it(monkeypatch):
    monkeypatch.setattr(aiohttp.ClientSession, "get", lambda self, url: _FakeAiohttpResponse(b"image-bytes"))
    client = FakeClient()
    server = client.add_server(FakeServer(id="srv-1"))
    receiver = _make_receiver(client)

    result = await receiver.create_emoji(CustomEmoji(native_id="e1", name="smile", image_url="https://cdn.example/e.png"))

    assert server.created_emoji_calls == [{"name": "smile", "image": b"image-bytes"}]
    assert result is not None
    assert result.name == "smile"


async def test_create_emoji_returns_none_on_http_failure(monkeypatch):
    monkeypatch.setattr(aiohttp.ClientSession, "get", lambda self, url: _FakeAiohttpResponse(b"image-bytes"))
    client = FakeClient()
    client.add_server(FakeServer(id="srv-1", raises=aiohttp.ClientResponseError(None, (), status=400)))
    receiver = _make_receiver(client)

    result = await receiver.create_emoji(CustomEmoji(native_id="e1", name="smile", image_url="https://cdn.example/e.png"))

    assert result is None
