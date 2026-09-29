"""Link-preview attachments on the Stoat receiver (issue #164): the source's
resolved media is re-uploaded and the link stripped by default, or skipped
with the link left for Stoat to unfurl when a rule prefers it."""

from __future__ import annotations

import aiohttp
import stoat

from stoat_discord_bridge.models import Attachment, StandardEdit
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


async def test_a_previews_text_is_relayed_along_with_its_media(monkeypatch):
    # issue #209: a fixupx-style embed's text replaces the stripped link
    monkeypatch.setattr(aiohttp.ClientSession, "get", lambda self, url: _FakeAiohttpResponse(b"img"))
    client = FakeClient()
    channel = client.add_channel(FakeChannel(id="42"))
    texted = Attachment(url=_PREVIEW.url, filename="abc.jpg", source_page_url=_PAGE, preview_text="the post")

    await _receiver(client).receive(_message(content_markdown=_PAGE, attachments=[texted]), target_channel_id="42")

    assert channel.sent[0]["content"] == f"<{_PAGE}>\n\nthe post"
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


# ------------------------------------------- late preview backfilled by an edit (issue #207)


def _late_preview_edit(content=_PAGE, preview=_PREVIEW):
    return StandardEdit(
        origin_connector_id="discord",
        origin_channel_id="d-100",
        origin_message_id="m1",
        new_content_markdown=content,
        new_attachments=[preview],
    )


async def test_edit_adds_a_late_previews_text_along_with_its_media(monkeypatch):
    monkeypatch.setattr(aiohttp.ClientSession, "get", lambda self, url: _FakeAiohttpResponse(b"img"))
    client = FakeClient()
    channel = client.add_channel(FakeChannel(id="42"))
    texted = Attachment(url=_PREVIEW.url, filename="abc.jpg", source_page_url=_PAGE, preview_text="the post")

    await _receiver(client).edit_message(
        target_channel_id="42", target_message_ids=["7"], edit=_late_preview_edit(preview=texted)
    )

    post = await channel.fetch_message("7")
    assert post.edits == [f"<{_PAGE}>\n\nthe post"]
    [[embed]] = post.edited_embeds
    assert embed.media == ("abc.jpg", b"img")


async def test_edit_adds_a_late_preview_as_an_embed_on_the_last_post(monkeypatch):
    # stoat.py's Message.edit takes no files, but a SendableEmbed's `media`
    # uploads a (filename, bytes) pair itself.
    monkeypatch.setattr(aiohttp.ClientSession, "get", lambda self, url: _FakeAiohttpResponse(b"img"))
    client = FakeClient()
    channel = client.add_channel(FakeChannel(id="42"))

    await _receiver(client).edit_message(
        target_channel_id="42", target_message_ids=["7", "8"], edit=_late_preview_edit(f"look {_PAGE}")
    )

    first, last = await channel.fetch_message("7"), await channel.fetch_message("8")
    assert (first.edits, first.edited_embeds) == (["look"], [])
    assert last.edits == ["​"]
    [[embed]] = last.edited_embeds
    assert isinstance(embed, stoat.SendableEmbed)
    assert embed.media == ("abc.jpg", b"img")


async def test_edit_leaves_the_post_alone_when_a_rule_prefers_stoats_own_preview(monkeypatch):
    monkeypatch.setattr(aiohttp.ClientSession, "get", lambda self, url: _FakeAiohttpResponse(b"img"))
    client = FakeClient()
    channel = client.add_channel(FakeChannel(id="42"))

    await _receiver(client, _Prefs({"instagram.com": "stoat"})).edit_message(
        target_channel_id="42", target_message_ids=["7"], edit=_late_preview_edit()
    )

    assert (await channel.fetch_message("7")).edits == []


async def test_edit_leaves_the_post_alone_when_the_preview_cant_be_fetched(monkeypatch):
    monkeypatch.setattr(aiohttp.ClientSession, "get", lambda self, url: _FakeAiohttpResponse(b"", status=404))
    client = FakeClient()
    channel = client.add_channel(FakeChannel(id="42"))

    await _receiver(client).edit_message(target_channel_id="42", target_message_ids=["7"], edit=_late_preview_edit())

    assert (await channel.fetch_message("7")).edits == []
