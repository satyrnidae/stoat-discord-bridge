from stoat_discord_bridge.storage.channel_mappings import ChannelMapping, ChannelMappingRepository


async def test_upsert_and_get_bridge_group(fake_db):
    repo = ChannelMappingRepository(fake_db)
    await repo.upsert(ChannelMapping(bridge_group="g1", connector_id="discord", channel_id="d1", channel_name="general"))

    assert await repo.get_bridge_group("discord", "d1") == "g1"
    assert await repo.get_bridge_group("discord", "nonexistent") is None


async def test_upsert_is_idempotent_by_connector_and_channel(fake_db):
    repo = ChannelMappingRepository(fake_db)
    await repo.upsert(ChannelMapping(bridge_group="g1", connector_id="discord", channel_id="d1", channel_name="general"))
    # re-upsert same (connector_id, channel_id) with a new name/group - should update in place, not duplicate
    await repo.upsert(ChannelMapping(bridge_group="g2", connector_id="discord", channel_id="d1", channel_name="renamed"))

    mapped = await repo.get_mapped_channels("g2")
    assert len(mapped) == 1
    assert mapped[0].channel_name == "renamed"
    assert await repo.get_bridge_group("discord", "d1") == "g2"


async def test_get_mapped_channels_returns_every_connector_in_group(fake_db):
    repo = ChannelMappingRepository(fake_db)
    await repo.upsert(ChannelMapping(bridge_group="g1", connector_id="discord", channel_id="d1", channel_name="general"))
    await repo.upsert(ChannelMapping(bridge_group="g1", connector_id="irc", channel_id="#general", channel_name="#general"))

    mapped = await repo.get_mapped_channels("g1")
    connector_ids = {m.connector_id for m in mapped}
    assert connector_ids == {"discord", "irc"}


async def test_get_all_for_connector(fake_db):
    repo = ChannelMappingRepository(fake_db)
    await repo.upsert(ChannelMapping(bridge_group="g1", connector_id="irc", channel_id="#a", channel_name="#a"))
    await repo.upsert(ChannelMapping(bridge_group="g2", connector_id="irc", channel_id="#b", channel_name="#b"))
    await repo.upsert(ChannelMapping(bridge_group="g1", connector_id="discord", channel_id="d1", channel_name="a"))

    irc_only = await repo.get_all_for_connector("irc")
    assert {m.channel_id for m in irc_only} == {"#a", "#b"}
