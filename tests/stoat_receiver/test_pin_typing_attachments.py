from __future__ import annotations

import aiohttp

from stoat_discord_bridge.models import Attachment
from tests.fakes.fake_stoat import FakeChannel, FakeClient
from tests.stoat_receiver.conftest import _FakeAiohttpResponse, _make_receiver, _message


# ---------------------------------------------------------------- set_pinned


async def test_set_pinned_pins_and_unpins_the_message():
    client = FakeClient()
    channel = client.add_channel(FakeChannel(id="c-1"))
    receiver = _make_receiver(client)

    await receiver.set_pinned(target_channel_id="c-1", target_message_id="m7", pinned=True)
    msg = await channel.fetch_message("m7")
    assert msg.pinned is True and msg.pin_calls == 1

    await receiver.set_pinned(target_channel_id="c-1", target_message_id="m7", pinned=False)
    assert msg.pinned is False and msg.unpin_calls == 1


async def test_set_pinned_is_a_noop_when_already_in_the_target_state():
    client = FakeClient()
    channel = client.add_channel(FakeChannel(id="c-1"))
    msg = await channel.fetch_message("m7")
    msg.pinned = True
    receiver = _make_receiver(client)

    await receiver.set_pinned(target_channel_id="c-1", target_message_id="m7", pinned=True)

    assert msg.pin_calls == 0


# ---------------------------------------------------------------- trigger_typing


async def test_trigger_typing_keeps_typing_then_ends_it():
    client = FakeClient()
    channel = client.add_channel(FakeChannel(id="c-1"))
    receiver = _make_receiver(client)
    receiver._TYPING_LINGER = 0.05
    receiver._TYPING_REFRESH = 0.01

    await receiver.trigger_typing(target_channel_id="c-1")
    await receiver._typing_tasks["c-1"]

    assert channel.typing_events[0] == "begin"
    assert channel.typing_events[-1] == "end"
    assert receiver._typing_tasks == {}


async def test_trigger_typing_reuses_the_running_loop_for_repeat_calls():
    client = FakeClient()
    client.add_channel(FakeChannel(id="c-1"))
    receiver = _make_receiver(client)
    receiver._TYPING_LINGER = 0.05
    receiver._TYPING_REFRESH = 0.01

    await receiver.trigger_typing(target_channel_id="c-1")
    task = receiver._typing_tasks["c-1"]
    await receiver.trigger_typing(target_channel_id="c-1")

    assert receiver._typing_tasks["c-1"] is task
    await task


async def test_stop_typing_cancels_the_loop_and_ends_the_indicator():
    client = FakeClient()
    channel = client.add_channel(FakeChannel(id="c-1"))
    receiver = _make_receiver(client)
    receiver._TYPING_LINGER = 5.0
    receiver._TYPING_REFRESH = 0.01

    await receiver.trigger_typing(target_channel_id="c-1")
    await receiver.stop_typing(target_channel_id="c-1")

    assert receiver._typing_tasks == {}
    assert receiver._typing_until == {}
    assert channel.typing_events[-1] == "end"


async def test_stop_typing_is_a_safe_noop_when_nothing_is_typing():
    client = FakeClient()
    channel = client.add_channel(FakeChannel(id="c-1"))
    receiver = _make_receiver(client)

    await receiver.stop_typing(target_channel_id="c-1")  # must not raise

    assert channel.typing_events == ["end"]


# ---------------------------------------------------------------- attachments (#39)


async def test_receive_reuploads_attachments_as_native_files(monkeypatch):
    monkeypatch.setattr(aiohttp.ClientSession, "get", lambda self, url: _FakeAiohttpResponse(b"img"))
    client = FakeClient()
    channel = client.add_channel(FakeChannel(id="42"))
    receiver = _make_receiver(client)

    await receiver.receive(
        _message(
            content_markdown="look",
            attachments=[Attachment(url="https://cdn.example/pic.png", filename="pic.png")],
        ),
        target_channel_id="42",
    )

    assert channel.sent[0]["content"] == "look"  # URL is not pasted into the text
    assert channel.sent[0]["attachments"] == [("pic.png", b"img")]


async def test_receive_sends_a_file_only_message_with_empty_content(monkeypatch):
    monkeypatch.setattr(aiohttp.ClientSession, "get", lambda self, url: _FakeAiohttpResponse(b"img"))
    client = FakeClient()
    channel = client.add_channel(FakeChannel(id="42"))
    receiver = _make_receiver(client)

    await receiver.receive(
        _message(content_markdown="", attachments=[Attachment(url="https://cdn.example/a.png")]),
        target_channel_id="42",
    )

    assert channel.sent[0]["content"] == ""
    assert channel.sent[0]["attachments"] == [("a.png", b"img")]


async def test_receive_falls_back_to_the_url_when_an_attachment_cant_be_downloaded(monkeypatch):
    monkeypatch.setattr(
        aiohttp.ClientSession, "get", lambda self, url: _FakeAiohttpResponse(b"", status=404)
    )
    client = FakeClient()
    channel = client.add_channel(FakeChannel(id="42"))
    receiver = _make_receiver(client)

    await receiver.receive(
        _message(content_markdown="hi", attachments=[Attachment(url="https://cdn.example/gone.png")]),
        target_channel_id="42",
    )

    assert channel.sent[0]["content"] == "hi\nhttps://cdn.example/gone.png"
    assert "attachments" not in channel.sent[0]
