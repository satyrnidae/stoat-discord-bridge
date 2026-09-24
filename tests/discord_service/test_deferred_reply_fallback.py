"""A deferred command's reply falls back to a plain channel post when the
interaction's followup token has expired (issue #159) - a long batched
`/mirror <noun> to <service> all` can outlive Discord's 15-minute window,
and the operator should still see the result rather than nothing."""

from __future__ import annotations

from types import SimpleNamespace

import discord

from stoat_discord_bridge.admin_commands import LinkError
from tests.discord_service.conftest import FakeInteraction, FakeLinker, _make_sender


def _expired_token() -> discord.HTTPException:
    return discord.NotFound(SimpleNamespace(status=404, reason="Not Found"), "Unknown Webhook")


async def test_followup_success_takes_no_fallback():
    sender = _make_sender(FakeLinker())
    interaction = FakeInteraction()

    await sender._handle_mirror_channel(interaction, "all", None)

    assert interaction.sent == ["ok"]
    assert interaction.channel_sent == []


async def test_expired_followup_falls_back_to_a_channel_post_mentioning_the_user():
    sender = _make_sender(FakeLinker())
    interaction = FakeInteraction(user_id=42)
    interaction.followup_error = _expired_token()

    await sender._handle_mirror_channel(interaction, "all", None)

    assert interaction.sent == []
    assert interaction.channel_sent == ["<@42> ok"]


async def test_expired_followup_with_no_channel_completes_silently():
    sender = _make_sender(FakeLinker())
    interaction = FakeInteraction()
    interaction.followup_error = _expired_token()
    interaction.channel = None

    await sender._handle_mirror_channel(interaction, "all", None)

    assert interaction.sent == []


async def test_failing_fallback_post_completes_silently():
    sender = _make_sender(FakeLinker())
    interaction = FakeInteraction()
    interaction.followup_error = _expired_token()
    interaction.channel_error = _expired_token()

    await sender._handle_mirror_channel(interaction, "all", None)

    assert interaction.sent == []
    assert interaction.channel_sent == []


async def test_link_error_reply_also_falls_back():
    class _RejectingLinker(FakeLinker):
        async def mirror_channel_all(self, **kwargs):
            raise LinkError("Stoat is busy.")

    sender = _make_sender(_RejectingLinker())
    interaction = FakeInteraction(user_id=42)
    interaction.followup_error = _expired_token()

    await sender._handle_mirror_channel(interaction, "all", None)

    assert interaction.channel_sent == ["<@42> Stoat is busy."]
