import pytest

from stoat_discord_bridge.admin_commands import ChannelLinker, ConnectorInfo, LinkError
from stoat_discord_bridge.storage.category_mappings import CategoryMapping, CategoryMappingRepository
from stoat_discord_bridge.storage.channel_mappings import ChannelMappingRepository


# ---------------------------------------------------------------- ChannelLinker.mirror_channel_from


def _channel_from_connectors(*, ensure_calls, source_category=None):
    async def ensure_channel(name, category=None, is_thread_category=False, category_parent_channel_id=None):
        ensure_calls.append((name, category))
        return f"stoat_{name}"

    async def resolve_channel_name(cid):
        return {"d1": "general"}.get(cid, cid)

    async def resolve_channel_category(cid):
        return source_category

    return {
        "discord": ConnectorInfo(
            id="discord",
            label="Discord",
            resolve_channel_name=resolve_channel_name,
            resolve_channel_category=resolve_channel_category,
        ),
        "stoat": ConnectorInfo(id="stoat", label="Stoat", ensure_channel=ensure_channel),
    }


async def test_mirror_channel_from_creates_the_local_channel_and_links(fake_db):
    ensure_calls: list = []
    connectors = _channel_from_connectors(ensure_calls=ensure_calls)
    channel_mappings = ChannelMappingRepository(fake_db)
    linker = ChannelLinker(channel_mappings, connectors)

    summary = await linker.mirror_channel_from(local_connector="stoat", source="discord", source_id="d1")

    assert "Linked Discord channel 'general'" in summary
    assert ensure_calls == [("general", None)]
    assert await channel_mappings.get_bridge_group("stoat", "stoat_general") is not None


async def test_mirror_channel_from_lands_in_the_linked_local_category(fake_db):
    ensure_calls: list = []
    connectors = _channel_from_connectors(ensure_calls=ensure_calls, source_category=("dcat", "Discord Team"))
    channel_mappings = ChannelMappingRepository(fake_db)
    category_mappings = CategoryMappingRepository(fake_db)
    # discord category `dcat` is already linked to stoat category "Stoat Team"
    await category_mappings.upsert(
        CategoryMapping(bridge_group="g1", connector_id="discord", category_id="dcat", category_name="Discord Team")
    )
    await category_mappings.upsert(
        CategoryMapping(bridge_group="g1", connector_id="stoat", category_id="scat", category_name="Stoat Team")
    )
    linker = ChannelLinker(channel_mappings, connectors, category_mappings)

    await linker.mirror_channel_from(local_connector="stoat", source="discord", source_id="d1")

    assert ensure_calls == [("general", "Stoat Team")]


async def test_mirror_channel_from_uses_source_category_name_when_unlinked(fake_db):
    ensure_calls: list = []
    connectors = _channel_from_connectors(ensure_calls=ensure_calls, source_category=("dcat", "Discord Team"))
    linker = ChannelLinker(ChannelMappingRepository(fake_db), connectors, CategoryMappingRepository(fake_db))

    await linker.mirror_channel_from(local_connector="stoat", source="discord", source_id="d1")

    assert ensure_calls == [("general", "Discord Team")]


async def test_mirror_channel_from_own_connector_raises(fake_db, connectors):
    linker = ChannelLinker(ChannelMappingRepository(fake_db), connectors)
    with pytest.raises(LinkError, match="from a connector to itself"):
        await linker.mirror_channel_from(local_connector="discord", source="discord", source_id="d1")


async def test_mirror_channel_from_unknown_source_raises(fake_db, connectors):
    linker = ChannelLinker(ChannelMappingRepository(fake_db), connectors)
    with pytest.raises(LinkError, match="isn't a known connector"):
        await linker.mirror_channel_from(local_connector="discord", source="nope", source_id="d1")