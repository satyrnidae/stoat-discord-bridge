"""Link-preview attachments on the Stoat receiver (issue #164): the source's
resolved media is re-uploaded and the link stripped by default, or skipped
with the link left for Stoat to unfurl when a rule prefers it."""

from __future__ import annotations

import aiohttp

from stoat_discord_bridge.models import Attachment
from stoat_discord_bridge.services.stoat_service import StoatReceiverService
from tests.fakes.fake_stoat import FakeChannel, FakeClient
from tests.stoat_receiver.conftest import _FakeAiohttpResponse, _FakeSender, _message

_PAGE = "https://www.instagram.com/p/abc"
_PREVIEW = Attachment(url="https://cdn.example/abc.jpg", filename="abc.jpg", source_page_url=_PAGE)


class _Prefs:
    def __init__(self, rules: dict[str, str]):
        self._rules = rules

    async def preferred_kind_for(self, url: str) -> str | None:
        return next((kind for sub, kind in self._rules.items() if sub in url), None)


def _receiver(client, prefs=None) -> StoatReceiverService:
    return StoatReceiverService(_FakeSender(client), attachment_preferences=prefs)


async def test_default_reuploads_the_preview_and_strips_the_link(monkeypatch):
    monkeypatch.setattr(aiohttp.ClientSession, "get", lambda self, url: _FakeAiohttpResponse(b"img"))
    client = FakeClient()
    channel = client.add_channel(FakeChannel(id="42"))

    await _receiver(client, _Prefs({"instagram.com": "discord"})).receive(
        _message(content_markdown=f"look {_PAGE}", attachments=[_PREVIEW]), target_channel_id="42"
    )

    assert channel.sent[0]["content"] == "look"
    assert channel.sent[0]["attachments"] == [("abc.jpg", b"img")]


async def test_a_stoat_rule_skips_the_preview_and_keeps_the_link(monkeypatch):
    fetched: list[str] = []

    def fake_get(self, url):
        fetched.append(url)
        return _FakeAiohttpResponse(b"img")

    monkeypatch.setattr(aiohttp.ClientSession, "get", fake_get)
    client = FakeClient()
    channel = client.add_channel(FakeChannel(id="42"))

    await _receiver(client, _Prefs({"instagram.com": "stoat"})).receive(
        _message(content_markdown=f"look {_PAGE}", attachments=[_PREVIEW]), target_channel_id="42"
    )

    assert channel.sent[0]["content"] == f"look {_PAGE}"
    assert "attachments" not in channel.sent[0]
    assert fetched == []
