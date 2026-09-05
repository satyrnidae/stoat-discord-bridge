from __future__ import annotations

import asyncio

import aiohttp

from stoat_discord_bridge.models import Attachment
from tests.fakes.fake_discord import FakeChannel, FakeClient, FakeFullMessage
from tests.discord_receiver.conftest import _FakeAiohttpResponse, _make_receiver, _message


# ---------------------------------------------------------------- set_pinned


async def test_set_pinned_pins_and_unpins_the_message():
    client = FakeClient()
    channel = client.add_channel(FakeChannel(id=42))
    receiver = _make_receiver(client)

    await receiver.set_pinned(target_channel_id="42", target_message_id="7", pinned=True)
    msg = await channel.fetch_message(7)
    assert msg.pinned is True
    assert msg.pin_calls == ["bridge pin sync"]

    await receiver.set_pinned(target_channel_id="42", target_message_id="7", pinned=False)
    assert msg.pinned is False
    assert msg.unpin_calls == ["bridge pin sync"]


async def test_set_pinned_is_a_noop_when_already_in_the_target_state():
    client = FakeClient()
    channel = client.add_channel(FakeChannel(id=42))
    channel.full_messages[7] = pinned_msg = FakeFullMessage(id=7, pinned=True)
    receiver = _make_receiver(client)

    await receiver.set_pinned(target_channel_id="42", target_message_id="7", pinned=True)

    assert pinned_msg.pin_calls == []


# ---------------------------------------------------------------- trigger_typing


async def test_trigger_typing_keeps_refreshing_then_lapses():
    client = FakeClient()
    channel = client.add_channel(FakeChannel(id=42))
    receiver = _make_receiver(client)
    receiver._TYPING_LINGER = 0.05
    receiver._TYPING_REFRESH = 0.01

    await receiver.trigger_typing(target_channel_id="42")
    await receiver._typing_tasks["42"]

    assert channel.typing_calls >= 1
    assert receiver._typing_tasks == {}


async def test_trigger_typing_reuses_the_running_loop_for_repeat_calls():
    client = FakeClient()
    client.add_channel(FakeChannel(id=42))
    receiver = _make_receiver(client)
    receiver._TYPING_LINGER = 0.05
    receiver._TYPING_REFRESH = 0.01

    await receiver.trigger_typing(target_channel_id="42")
    task = receiver._typing_tasks["42"]
    await receiver.trigger_typing(target_channel_id="42")

    assert receiver._typing_tasks["42"] is task
    await task


async def test_trigger_typing_swallows_a_missing_channel():
    receiver = _make_receiver(FakeClient())

    await receiver.trigger_typing(target_channel_id="999")
    await receiver._typing_tasks["999"]  # must not raise


async def test_stop_typing_halts_the_keep_alive_loop():
    client = FakeClient()
    channel = client.add_channel(FakeChannel(id=42))
    receiver = _make_receiver(client)
    receiver._TYPING_LINGER = 5.0
    receiver._TYPING_REFRESH = 0.01

    await receiver.trigger_typing(target_channel_id="42")
    await receiver.stop_typing(target_channel_id="42")
    calls_after_stop = channel.typing_calls
    await asyncio.sleep(0.05)

    assert receiver._typing_tasks == {}
    assert channel.typing_calls == calls_after_stop  # no further refreshes


async def test_stop_typing_is_a_safe_noop_when_nothing_is_typing():
    receiver = _make_receiver(FakeClient())

    await receiver.stop_typing(target_channel_id="42")  # must not raise


# ---------------------------------------------------------------- attachments (#39)


async def test_receive_reuploads_attachments_as_native_files(monkeypatch):
    monkeypatch.setattr(aiohttp.ClientSession, "get", lambda self, url: _FakeAiohttpResponse(b"img"))
    client = FakeClient()
    channel = client.add_channel(FakeChannel(id=42))
    receiver = _make_receiver(client)

    ids = await receiver.receive(
        _message(
            content_markdown="look at this",
            attachments=[
                Attachment(url="https://cdn.discordapp.com/attachments/1/2/pic.png?ex=abc", filename="pic.png")
            ],
        ),
        target_channel_id="42",
    )

    webhook = channel.created_webhooks[0]
    assert len(webhook.sent) == 1
    assert webhook.sent[0]["content"] == "look at this"  # URL is not pasted into the text
    assert webhook.sent[0]["files"] == [("pic.png", b"img")]
    assert ids == ["1000"]


async def test_receive_sends_a_file_only_message_with_empty_content(monkeypatch):
    monkeypatch.setattr(aiohttp.ClientSession, "get", lambda self, url: _FakeAiohttpResponse(b"img"))
    client = FakeClient()
    channel = client.add_channel(FakeChannel(id=42))
    receiver = _make_receiver(client)

    await receiver.receive(
        _message(content_markdown="", attachments=[Attachment(url="https://cdn.example/a.png")]),
        target_channel_id="42",
    )

    webhook = channel.created_webhooks[0]
    assert webhook.sent[0]["content"] == ""
    assert webhook.sent[0]["files"] == [("a.png", b"img")]


async def test_receive_falls_back_to_the_url_when_an_attachment_cant_be_downloaded(monkeypatch):
    monkeypatch.setattr(
        aiohttp.ClientSession, "get", lambda self, url: _FakeAiohttpResponse(b"", status=404)
    )
    client = FakeClient()
    channel = client.add_channel(FakeChannel(id=42))
    receiver = _make_receiver(client)

    await receiver.receive(
        _message(content_markdown="hi", attachments=[Attachment(url="https://cdn.example/gone.png")]),
        target_channel_id="42",
    )

    webhook = channel.created_webhooks[0]
    assert webhook.sent[0]["content"] == "hi\nhttps://cdn.example/gone.png"
    assert "files" not in webhook.sent[0]


async def test_receive_attaches_files_to_the_last_chunk_of_a_split_message(monkeypatch):
    monkeypatch.setattr(aiohttp.ClientSession, "get", lambda self, url: _FakeAiohttpResponse(b"img"))
    monkeypatch.setattr("stoat_discord_bridge.services.discord_service._CONTENT_LIMIT", 5)
    client = FakeClient()
    channel = client.add_channel(FakeChannel(id=42))
    receiver = _make_receiver(client)

    await receiver.receive(
        _message(content_markdown="abcdefghij", attachments=[Attachment(url="https://cdn.example/a.png")]),
        target_channel_id="42",
    )

    webhook = channel.created_webhooks[0]
    assert [c["content"] for c in webhook.sent] == ["abcde", "fghij"]
    assert "files" not in webhook.sent[0]
    assert webhook.sent[1]["files"] == [("a.png", b"img")]
