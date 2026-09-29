"""Link-preview attachments on the Discord receiver (issue #164): the
source's resolved media is re-uploaded and the link stripped by default, or
skipped with the link left for Discord to unfurl when a rule prefers it."""

from __future__ import annotations

import aiohttp

from stoat_discord_bridge.models import Attachment
from stoat_discord_bridge.services.discord_service import DiscordReceiverService
from tests.discord_receiver.conftest import _edit, _FakeAiohttpResponse, _message
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


# ------------------------------------------- late preview backfilled by an edit (issue #207)


async def test_edit_adds_a_late_preview_to_the_last_post_and_strips_the_link(monkeypatch):
    monkeypatch.setattr(aiohttp.ClientSession, "get", lambda self, url: _FakeAiohttpResponse(b"img"))
    client = FakeClient()
    channel = client.add_channel(FakeChannel(id=42))
    receiver = _receiver(client)

    await receiver.edit_message(
        target_channel_id="42",
        target_message_ids=["1000", "1001"],
        edit=_edit(new_content_markdown=_PAGE, new_attachments=[_PREVIEW]),
    )

    webhook = channel.created_webhooks[0]
    assert webhook.edited[0] == {"message_id": 1000, "content": "​", "thread": None}
    assert webhook.edited[1] == {
        "message_id": 1001, "content": "", "thread": None, "attachments": [("abc.jpg", b"img")]
    }


async def test_edit_adds_a_late_previews_text_along_with_its_media(monkeypatch):
    # issue #209: a late unfurl's text isn't lost when its link is stripped
    monkeypatch.setattr(aiohttp.ClientSession, "get", lambda self, url: _FakeAiohttpResponse(b"img"))
    client = FakeClient()
    channel = client.add_channel(FakeChannel(id=42))
    texted = Attachment(url=_PREVIEW.url, filename="abc.jpg", source_page_url=_PAGE, preview_text="the post")

    await _receiver(client).edit_message(
        target_channel_id="42",
        target_message_ids=["1000"],
        edit=_edit(new_content_markdown=f"<{_PAGE}>", new_attachments=[texted]),
    )

    [edited] = channel.created_webhooks[0].edited
    assert edited["content"] == f"<{_PAGE}>\n\nthe post"
    assert edited["attachments"] == [("abc.jpg", b"img")]


async def test_edit_keeps_the_posts_existing_attachments(monkeypatch):
    monkeypatch.setattr(aiohttp.ClientSession, "get", lambda self, url: _FakeAiohttpResponse(b"img"))
    client = FakeClient()
    channel = client.add_channel(FakeChannel(id=42))
    receiver = _receiver(client)
    existing = object()
    await receiver.edit_message(target_channel_id="42", target_message_ids=["1000"], edit=_edit())
    webhook = channel.created_webhooks[0]
    webhook.existing_attachments[1000] = [existing]

    await receiver.edit_message(
        target_channel_id="42",
        target_message_ids=["1000"],
        edit=_edit(new_content_markdown=f"look {_PAGE}", new_attachments=[_PREVIEW]),
    )

    assert webhook.edited[-1]["content"] == "look"
    assert webhook.edited[-1]["attachments"] == [existing, ("abc.jpg", b"img")]


async def test_edit_leaves_the_post_alone_when_a_rule_prefers_discords_own_preview(monkeypatch):
    monkeypatch.setattr(aiohttp.ClientSession, "get", lambda self, url: _FakeAiohttpResponse(b"img"))
    client = FakeClient()
    channel = client.add_channel(FakeChannel(id=42))
    receiver = _receiver(client, _Prefs({"instagram.com": "discord"}))

    await receiver.edit_message(
        target_channel_id="42",
        target_message_ids=["1000"],
        edit=_edit(new_content_markdown=_PAGE, new_attachments=[_PREVIEW]),
    )

    assert channel.created_webhooks == [] or channel.created_webhooks[0].edited == []


async def test_edit_leaves_the_post_alone_when_the_preview_cant_be_fetched(monkeypatch):
    monkeypatch.setattr(aiohttp.ClientSession, "get", lambda self, url: _FakeAiohttpResponse(b"", status=404))
    client = FakeClient()
    channel = client.add_channel(FakeChannel(id=42))
    receiver = _receiver(client)

    await receiver.edit_message(
        target_channel_id="42",
        target_message_ids=["1000"],
        edit=_edit(new_content_markdown=_PAGE, new_attachments=[_PREVIEW]),
    )

    assert channel.created_webhooks == [] or channel.created_webhooks[0].edited == []
