"""A deferred command's reply replaces Discord's "is thinking..." placeholder
instead of leaving it stuck; past a minute the placeholder says the command is
still working and the result arrives as a new followup (issue #201)."""

from __future__ import annotations

import asyncio

import pytest

from stoat_discord_bridge.admin_commands import LinkError
from stoat_discord_bridge.services import long_running
from stoat_discord_bridge.services.long_running import STILL_WORKING_TEXT
from tests.discord_service.conftest import FakeInteraction, FakeLinker, _make_sender


@pytest.fixture
def fast_threshold(monkeypatch):
    monkeypatch.setattr(long_running, "SLOW_AFTER_SECONDS", 0.02)


class _SlowLinker(FakeLinker):
    async def mirror_channel_all(self, **kwargs):
        await asyncio.sleep(0.1)
        return "ok"


class _SlowRejectingLinker(FakeLinker):
    async def mirror_channel_all(self, **kwargs):
        await asyncio.sleep(0.1)
        raise LinkError("Stoat is busy.")


async def test_fast_reply_replaces_the_thinking_placeholder():
    sender = _make_sender(FakeLinker())
    interaction = FakeInteraction()

    await sender._handle_mirror_channel(interaction, "all", None)

    assert interaction.original_edits == ["ok"]
    assert interaction.sent == ["ok"]


async def test_slow_reply_flags_the_placeholder_then_posts_a_new_followup(fast_threshold):
    sender = _make_sender(_SlowLinker())
    interaction = FakeInteraction()

    await sender._handle_mirror_channel(interaction, "all", None)

    assert interaction.original_edits == [STILL_WORKING_TEXT]
    assert interaction.sent == [STILL_WORKING_TEXT, "ok"]


async def test_slow_link_error_is_posted_as_a_new_followup(fast_threshold):
    sender = _make_sender(_SlowRejectingLinker())
    interaction = FakeInteraction()

    await sender._handle_mirror_channel(interaction, "all", None)

    assert interaction.original_edits == [STILL_WORKING_TEXT]
    assert interaction.sent == [STILL_WORKING_TEXT, "Stoat is busy."]


async def test_non_deferring_handler_is_deferred_before_the_linker_runs():
    sender = _make_sender(FakeLinker())
    interaction = FakeInteraction()

    await sender._handle_link_channel(interaction, "stoat", "01ABC", None)

    assert interaction.deferred is True
    assert interaction.original_edits == ["ok"]
