"""Link-preview attachments on the Discord receiver (issue #164): the
source's resolved media is re-uploaded and the link stripped by default, or
skipped with the link left for Discord to unfurl when a rule prefers it."""

from __future__ import annotations

import aiohttp

from stoat_discord_bridge.models import Attachment
from stoat_discord_bridge.services.discord_service import DiscordReceiverService
from tests.discord_receiver.conftest import _FakeAiohttpResponse, _message
from tests.fakes.fake_discord import FakeChannel, FakeClient

_PAGE = "https://www.instagram.com/p/abc"
_PREVIEW = Attachment(url="https://cdn.example/abc.jpg", filename="abc.jpg", source_page_url=_PAGE)


class _Prefs:
    def __init__(self, rules: dict[str, str]):
        self._rules = rules

    async def preferred_kind_for(self, url: str) -> str | None:
        return next((kind for sub, kind in self._rules.items() if sub in url), None)


def _receiver(client, prefs=None) -> DiscordReceiverService:
    return DiscordReceiverService(client, guild_id=123, connector_id="discord", attachment_preferences=prefs)


async def test_default_reuploads_the_preview_and_strips_the_link(monkeypatch):
    fetched: list[str] = []

    def fake_get(self, url):
        fetched.append(url)
        return _FakeAiohttpResponse(b"img")

    monkeypatch.setattr(aiohttp.ClientSession, "get", fake_get)
    client = FakeClient()
    channel = client.add_channel(FakeChannel(id=42))

    await _receiver(client, _Prefs({})).receive(
        _message(content_markdown=f"look {_PAGE}", attachments=[_PREVIEW]), target_channel_id="42"
    )

    sent = channel.created_webhooks[0].sent[0]
    assert sent["content"] == "look"
    assert sent["files"] == [("abc.jpg", b"img")]
    assert fetched == [_PREVIEW.url]


async def test_a_discord_rule_skips_the_preview_and_keeps_the_link(monkeypatch):
    fetched: list[str] = []

    def fake_get(self, url):
        fetched.append(url)
        return _FakeAiohttpResponse(b"img")

    monkeypatch.setattr(aiohttp.ClientSession, "get", fake_get)
    client = FakeClient()
    channel = client.add_channel(FakeChannel(id=42))

    await _receiver(client, _Prefs({"instagram.com": "discord"})).receive(
        _message(content_markdown=f"look {_PAGE}", attachments=[_PREVIEW]), target_channel_id="42"
    )

    sent = channel.created_webhooks[0].sent[0]
    assert sent["content"] == f"look {_PAGE}"
    assert "files" not in sent
    assert fetched == []
