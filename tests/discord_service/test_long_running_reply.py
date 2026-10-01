"""A deferred command's reply replaces Discord's "is thinking..." placeholder
instead of leaving it stuck; past a minute the placeholder says the command is
still working (issue #201), and the result is still edited into that same
message rather than sent as a second one (issue #197)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import discord
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


async def test_slow_reply_flags_the_placeholder_then_edits_the_result_into_it(fast_threshold):
    sender = _make_sender(_SlowLinker())
    interaction = FakeInteraction()

    await sender._handle_mirror_channel(interaction, "all", None)

    assert interaction.original_edits == [STILL_WORKING_TEXT, "ok"]
    assert interaction.sent == interaction.original_edits  # no second message


async def test_slow_link_error_is_edited_into_the_placeholder(fast_threshold):
    sender = _make_sender(_SlowRejectingLinker())
    interaction = FakeInteraction()

    await sender._handle_mirror_channel(interaction, "all", None)

    assert interaction.original_edits == [STILL_WORKING_TEXT, "Stoat is busy."]
    assert interaction.sent == interaction.original_edits


async def test_slow_reply_falls_back_to_a_new_followup_if_the_placeholder_is_gone(fast_threshold):
    interaction = FakeInteraction()

    class _PlaceholderDeletedLinker(FakeLinker):
        async def mirror_channel_all(self, **kwargs):
            await asyncio.sleep(0.1)

            async def gone(**_kwargs):
                raise discord.NotFound(SimpleNamespace(status=404, reason="Not Found"), "Unknown Message")

            interaction.edit_original_response = gone
            return "ok"

    sender = _make_sender(_PlaceholderDeletedLinker())

    await sender._handle_mirror_channel(interaction, "all", None)

    assert interaction.original_edits == [STILL_WORKING_TEXT]
    assert interaction.sent == [STILL_WORKING_TEXT, "ok"]


async def test_non_deferring_handler_is_deferred_before_the_linker_runs():
    sender = _make_sender(FakeLinker())
    interaction = FakeInteraction()

    await sender._handle_link_channel(interaction, "stoat", "01ABC", None)

    assert interaction.deferred is True
    assert interaction.original_edits == ["ok"]
