"""`/mirror channel` syncing the source channel's metadata onto the linked
destination channel, matched or created (issue #195)."""

from stoat_discord_bridge.admin_commands import ChannelLinker, ConnectorInfo
from stoat_discord_bridge.models import ChannelMetadata
from stoat_discord_bridge.storage.channel_mappings import ChannelMappingRepository

_META = ChannelMetadata(description="the source topic", nsfw=True, slowmode_delay=30)


def _connectors(applied, *, describe=True, apply_raises=None):
    async def describe_channel(channel_id):
        return _META

    async def ensure_channel(name, category=None, is_thread_category=False, category_parent_channel_id=None, **_kw):
        return f"stoat_{name}", True

    async def apply_channel_metadata(channel_id, metadata):
        if apply_raises is not None:
            raise apply_raises
        applied.append((channel_id, metadata))

    return {
        "discord": ConnectorInfo(
            id="discord", label="Discord", describe_channel=describe_channel if describe else None
        ),
        "irc": ConnectorInfo(id="irc", label="IRC"),
        "stoat": ConnectorInfo(
            id="stoat", label="Stoat", ensure_channel=ensure_channel, apply_channel_metadata=apply_channel_metadata
        ),
    }


async def test_mirror_channel_applies_source_metadata_to_the_linked_channel(fake_db):
    applied = []
    linker = ChannelLinker(ChannelMappingRepository(fake_db), _connectors(applied))

    summary = await linker.mirror_channel(
        local_connector="discord", local_channel_id="d1", local_channel_name="general", destination="stoat"
    )

    assert "Linked" in summary
    assert applied == [("stoat_general", _META)]


async def test_mirror_channel_skips_apply_without_source_metadata(fake_db):
    applied = []
    linker = ChannelLinker(ChannelMappingRepository(fake_db), _connectors(applied, describe=False))

    await linker.mirror_channel(
        local_connector="discord", local_channel_id="d1", local_channel_name="general", destination="stoat"
    )

    assert applied == []


async def test_mirror_channel_survives_a_raising_apply_channel_metadata(fake_db):
    channel_mappings = ChannelMappingRepository(fake_db)
    linker = ChannelLinker(channel_mappings, _connectors([], apply_raises=RuntimeError("boom")))

    summary = await linker.mirror_channel(
        local_connector="discord", local_channel_id="d1", local_channel_name="general", destination="stoat"
    )

    assert "Linked" in summary
    assert await channel_mappings.get_bridge_group("stoat", "stoat_general") is not None


async def test_mirror_channel_never_applies_metadata_to_a_channel_linked_elsewhere(fake_db):
    # The same-named match is another group's channel (issue #184), so it's
    # skipped for `general-2` - only that one may be touched.
    applied = []
    linker = ChannelLinker(ChannelMappingRepository(fake_db), _connectors(applied))
    await linker.link_channel(
        local_connector="stoat", local_channel_id="stoat_general", local_channel_name="general",
        source="irc", source_id="#other", destination_id=None,
    )

    await linker.mirror_channel(
        local_connector="discord", local_channel_id="d1", local_channel_name="general", destination="stoat"
    )

    assert applied == [("stoat_general-2", _META)]


async def test_mirror_channel_already_synced_does_not_apply_metadata(fake_db):
    applied = []
    linker = ChannelLinker(ChannelMappingRepository(fake_db), _connectors(applied))
    await linker.mirror_channel(
        local_connector="discord", local_channel_id="d1", local_channel_name="general", destination="stoat"
    )
    applied.clear()

    summary = await linker.mirror_channel(
        local_connector="discord", local_channel_id="d1", local_channel_name="general", destination="stoat"
    )

    assert "already synced" in summary
    assert applied == []


async def test_mirror_channel_for_thread_applies_source_metadata(fake_db):
    applied = []
    linker = ChannelLinker(ChannelMappingRepository(fake_db), _connectors(applied))

    _, finish = await linker.mirror_channel_for_thread(
        local_connector="discord",
        local_channel_id="d1",
        local_channel_name="Test Thread",
        destination="stoat",
        local_channel_category="Announcements",
        category_from_channel_id="d-parent",
    )

    assert applied == [("stoat_Test Thread", _META)]
    assert finish is not None
