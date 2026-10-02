"""DiscordSenderService.entity_exists - the strict existence check a stale
link is pruned on (issue #217). False only for a definite "gone"; anything
it can't be sure of is None."""

from __future__ import annotations

from types import SimpleNamespace

from tests.discord_sender_dispatch.conftest import _make_sender, _Recorder
from tests.fakes.fake_discord import (
    FakeChannel,
    FakeClient,
    FakeGuild,
    FakeUser,
    discord_forbidden,
    discord_not_found,
)


def _sender(client: FakeClient):
    return _make_sender(_Recorder(), client)


async def test_channel_in_cache_exists():
    client = FakeClient()
    client.add_channel(FakeChannel(id=42, name="general"))

    assert await _sender(client).entity_exists("channel", "42") is True


async def test_channel_that_404s_is_gone():
    assert await _sender(FakeClient()).entity_exists("channel", "42") is False


async def test_channel_the_bot_cant_see_still_exists():
    client = FakeClient()

    async def fetch_channel(_channel_id):
        raise discord_forbidden()

    client.fetch_channel = fetch_channel

    assert await _sender(client).entity_exists("channel", "42") is True


async def test_channel_lookup_that_fails_otherwise_cant_tell():
    client = FakeClient()

    async def fetch_channel(_channel_id):
        raise RuntimeError("gateway hiccup")

    client.fetch_channel = fetch_channel

    assert await _sender(client).entity_exists("channel", "42") is None


async def test_user_exists_or_is_gone():
    client = FakeClient()
    client.add_user(FakeUser(id=7, display_name="someone"))
    sender = _sender(client)

    assert await sender.entity_exists("user", "7") is True
    assert await sender.entity_exists("user", "8") is False


async def test_role_and_emoji_read_the_live_guild_cache():
    client = FakeClient()
    guild = client.add_guild(FakeGuild(id=123))
    guild.add_role(SimpleNamespace(id=5, name="mods"))
    guild.add_emoji(SimpleNamespace(id=9, name="blobwave"))
    sender = _sender(client)

    assert await sender.entity_exists("role", "5") is True
    assert await sender.entity_exists("role", "6") is False
    assert await sender.entity_exists("emoji", "9") is True
    assert await sender.entity_exists("emoji", "10") is False


async def test_role_and_emoji_cant_tell_without_the_guild():
    sender = _sender(FakeClient())

    assert await sender.entity_exists("role", "5") is None
    assert await sender.entity_exists("emoji", "9") is None


async def test_bad_id_or_unknown_kind_cant_tell():
    client = FakeClient()
    client.add_guild(FakeGuild(id=123))
    sender = _sender(client)

    assert await sender.entity_exists("channel", "not-a-number") is None
    assert await sender.entity_exists("category", "5") is None


async def test_not_found_helper_is_a_real_404():
    # Guards the fake: the "gone" tests above rely on FakeClient raising it.
    assert discord_not_found().status == 404
