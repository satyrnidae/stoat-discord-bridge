"""Regression test for bridge.py's run() wiring itself (issue #142) - a
missing keyword argument in run()'s real ConnectorInfo(...) construction
silently disabled '/mirror channel with history' for every deployment (even
plain Discord <-> Stoat), and no existing with_history test caught it
because they all build ConnectorInfo by hand
(tests/admin_commands/test_channel_mirror_with_history.py etc.) rather than
through run()'s actual wiring. This drives run() itself (with every
network-touching dependency it constructs replaced by a fake/mock) and reads
back the real ConnectorInfo instances it built.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from stoat_discord_bridge import bridge as bridge_module
from stoat_discord_bridge.config import (
    BridgeConfig,
    DiscordConnectorConfig,
    MongoConfig,
    StoatConnectorConfig,
    VoiceConfig,
)


def _fake_sender() -> MagicMock:
    sender = MagicMock()
    sender.start = AsyncMock()
    sender.close = AsyncMock()
    sender.fetch_history = AsyncMock(return_value=[])
    return sender


async def _run_with_fakes(fake_db):
    """Run run() with every network-touching dependency faked. Returns the
    ConnectorInfo instances it built, keyed by id, plus the sender-class mocks
    so a test can inspect what each sender was constructed with."""
    config = BridgeConfig(
        discord=[DiscordConnectorConfig(id="discord", label="Discord", guild_id=1, bot_token="t")],
        stoat=[StoatConnectorConfig(id="stoat", label="Stoat", server_id="s", api_url="https://x", bot_token="t")],
        irc=[],
        mongo=MongoConfig(uri="mongodb://unused", db_name="unused"),
        # Skip VoiceBridgeCoordinator's periodic refresh loop entirely - not
        # what this test is about, and it's real (not mocked) machinery.
        voice=VoiceConfig(enabled=False),
    )

    discord_sender_cls = MagicMock(return_value=_fake_sender())
    stoat_sender_cls = MagicMock(return_value=_fake_sender())
    discord_receiver = MagicMock(close=AsyncMock())
    stoat_receiver = MagicMock(close=AsyncMock())

    real_connector_info = bridge_module.ConnectorInfo
    built: dict[str, object] = {}

    def _recording_connector_info(**kwargs):
        info = real_connector_info(**kwargs)
        built[kwargs["id"]] = info
        return info

    mongo_store = MagicMock()
    mongo_store.db = fake_db
    mongo_store.close = MagicMock()

    health_runner = MagicMock()
    health_runner.cleanup = AsyncMock()

    with (
        patch.object(bridge_module, "MongoStore", return_value=mongo_store),
        patch.object(bridge_module, "start_health_server", AsyncMock(return_value=health_runner)),
        patch.object(bridge_module, "DiscordSenderService", discord_sender_cls),
        patch.object(bridge_module, "StoatSenderService", stoat_sender_cls),
        patch.object(bridge_module, "DiscordReceiverService", return_value=discord_receiver),
        patch.object(bridge_module, "StoatReceiverService", return_value=stoat_receiver),
        patch.object(bridge_module, "ConnectorInfo", side_effect=_recording_connector_info),
    ):
        await bridge_module.run(config)

    return built, discord_sender_cls, stoat_sender_cls


async def test_run_wires_fetch_history_into_discord_and_stoat_connector_info(fake_db):
    built, discord_sender_cls, stoat_sender_cls = await _run_with_fakes(fake_db)

    assert built["discord"].fetch_history is discord_sender_cls.return_value.fetch_history
    assert built["stoat"].fetch_history is stoat_sender_cls.return_value.fetch_history
    # supports_history is what ChannelLinker's with_history gate actually
    # reads - issue #142 broke it end to end for both connectors.
    assert built["discord"].supports_history is True
    assert built["stoat"].supports_history is True


async def test_run_wires_emoji_rename_sync_into_the_discord_sender_only(fake_db):
    _built, discord_sender_cls, stoat_sender_cls = await _run_with_fakes(fake_db)

    on_emoji_renamed = discord_sender_cls.call_args.kwargs["on_emoji_renamed"]
    assert on_emoji_renamed.__func__ is bridge_module.BridgeCoordinator.handle_emoji_renamed
    assert "on_emoji_renamed" not in stoat_sender_cls.call_args.kwargs
