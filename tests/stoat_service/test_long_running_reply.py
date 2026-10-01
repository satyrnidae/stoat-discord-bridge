"""A Stoat command posts a placeholder up front and edits it into the result;
past a minute the placeholder says the command is still working (issue #201),
and the result is still edited into that same message rather than sent as a
second one (issue #197)."""

from __future__ import annotations

import asyncio

import pytest

from stoat_discord_bridge.admin_commands import LinkError
from stoat_discord_bridge.services import long_running
from stoat_discord_bridge.services.long_running import STILL_WORKING_TEXT
from tests.fakes.fake_stoat import FakeChannel, FakeSentMessage
from tests.stoat_service.conftest import FakeLinker, _make_ctx, _make_sender


@pytest.fixture
def fast_threshold(monkeypatch):
    monkeypatch.setattr(long_running, "SLOW_AFTER_SECONDS", 0.02)


class _SlowLinker(FakeLinker):
    async def mirror_channel(self, **kwargs):
        await asyncio.sleep(0.1)
        return "mirrored ok"


class _SlowRejectingLinker(FakeLinker):
    async def mirror_channel(self, **kwargs):
        await asyncio.sleep(0.1)
        raise LinkError("Discord is busy.")


def _ctx():
    return _make_ctx(channel=FakeChannel(id="c1", name="general"))


async def test_fast_reply_edits_the_placeholder_in_place():
    sender = _make_sender(linker=FakeLinker())
    ctx = _ctx()

    await sender._mirror_channel(ctx, "discord", "general")

    assert [m["content"] for m in ctx.channel.sent] == ["mirrored ok"]
    assert ctx.channel.sent[0]["edits"] == ["mirrored ok"]


async def test_slow_reply_flags_the_placeholder_then_edits_the_result_into_it(fast_threshold):
    sender = _make_sender(linker=_SlowLinker())
    ctx = _ctx()

    await sender._mirror_channel(ctx, "discord", "general")

    # one message throughout (issue #197)
    assert [m["content"] for m in ctx.channel.sent] == ["mirrored ok"]
    assert ctx.channel.sent[0]["edits"] == [STILL_WORKING_TEXT, "mirrored ok"]


async def test_slow_link_error_is_edited_into_the_placeholder(fast_threshold):
    sender = _make_sender(linker=_SlowRejectingLinker())
    ctx = _ctx()

    await sender._mirror_channel(ctx, "discord", "general")

    assert [m["content"] for m in ctx.channel.sent] == ["Discord is busy."]
    assert ctx.channel.sent[0]["edits"] == [STILL_WORKING_TEXT, "Discord is busy."]


async def test_slow_reply_falls_back_to_a_new_message_if_the_placeholder_is_gone(fast_threshold, monkeypatch):
    real_edit = FakeSentMessage.edit

    async def edit(self, *, content=None, **kwargs):
        if content != STILL_WORKING_TEXT:
            raise RuntimeError("message deleted")
        return await real_edit(self, content=content, **kwargs)

    monkeypatch.setattr(FakeSentMessage, "edit", edit)
    sender = _make_sender(linker=_SlowLinker())
    ctx = _ctx()

    await sender._mirror_channel(ctx, "discord", "general")

    assert [m["content"] for m in ctx.channel.sent] == [STILL_WORKING_TEXT, "mirrored ok"]
    # the fallback reply is kept out of the relay too
    assert {"1", "2"} <= set(sender._command_message_ids)


async def test_failed_placeholder_still_runs_the_command():
    linker = FakeLinker()
    sender = _make_sender(linker=linker)
    ctx = _ctx()
    real_send = ctx.send
    calls = 0

    async def flaky_send(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("rate limited")
        return await real_send(*args, **kwargs)

    ctx.send = flaky_send

    await sender._mirror_channel(ctx, "discord", "general")

    assert [m["content"] for m in ctx.channel.sent] == ["mirrored ok"]


async def test_placeholder_is_never_relayed(fast_threshold):
    sender = _make_sender(linker=_SlowLinker())
    ctx = _ctx()

    await sender._mirror_channel(ctx, "discord", "general")

    assert "1" in sender._command_message_ids
