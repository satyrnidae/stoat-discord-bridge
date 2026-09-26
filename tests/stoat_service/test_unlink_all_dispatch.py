"""Stoat's `/unlink all <service|all>` (issue #181)."""

from __future__ import annotations

from stoat_discord_bridge.admin_commands import ChannelLinker, ConnectorInfo, UserLinker
from stoat_discord_bridge.storage.channel_mappings import ChannelMappingRepository
from stoat_discord_bridge.storage.user_mappings import UserMappingRepository
from tests.stoat_service.conftest import _make_ctx, _make_sender


def _real_linkers(fake_db):
    connectors = {
        "discord": ConnectorInfo(id="discord", label="Discord"),
        "stoat": ConnectorInfo(id="stoat", label="Stoat"),
    }
    return ChannelLinker(ChannelMappingRepository(fake_db), connectors), UserLinker(
        UserMappingRepository(fake_db), connectors
    )


async def test_unlink_all_unlinks_every_configured_kind(fake_db):
    channel_linker, user_linker = _real_linkers(fake_db)
    await channel_linker.link_channel(
        local_connector="stoat", local_channel_id="s1", local_channel_name="general",
        source="discord", source_id="d1", destination_id=None,
    )
    await user_linker.link_user(local_connector="stoat", local_user_id="alice", source="discord", source_user_id="111")
    sender = _make_sender(linker=channel_linker, user_linker=user_linker)
    ctx = _make_ctx()

    await sender._unlink_all(ctx, "discord")

    reply = ctx.channel.sent[0]["content"]
    assert "Channels:\nUnlinked Discord from 1 bridge group(s) on Stoat:" in reply
    assert "Users:\nUnlinked Discord from 1 link group(s) on Stoat:" in reply
    assert await ChannelMappingRepository(fake_db).get_bridge_group("discord", "d1") is None


async def test_unlink_all_without_service_asks_for_one(fake_db):
    channel_linker, user_linker = _real_linkers(fake_db)
    sender = _make_sender(linker=channel_linker, user_linker=user_linker)
    ctx = _make_ctx()

    await sender._unlink_all(ctx, None)

    assert "explicit service" in ctx.channel.sent[0]["content"]


async def test_unlink_all_needs_admin(fake_db):
    channel_linker, _ = _real_linkers(fake_db)
    sender = _make_sender(linker=channel_linker)
    ctx = _make_ctx(manage_server=False)

    await sender._unlink_all(ctx, "all")

    assert ctx.channel.sent[0]["content"] == "You need the Manage Server permission to do that."


async def test_unlink_all_without_a_configured_linker():
    sender = _make_sender(linker=None)
    ctx = _make_ctx()

    await sender._unlink_all(ctx, "all")

    assert ctx.channel.sent[0]["content"] == "Linking isn't configured."
