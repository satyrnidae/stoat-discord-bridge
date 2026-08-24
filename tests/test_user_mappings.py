from stoat_discord_bridge.storage.user_mappings import UserMapping, UserMappingRepository


async def test_upsert_and_get_link_group(fake_db):
    repo = UserMappingRepository(fake_db)
    await repo.upsert(UserMapping(link_group="g1", connector_id="discord", user_id="111", display_name="111"))

    assert await repo.get_link_group("discord", "111") == "g1"
    assert await repo.get_link_group("discord", "nonexistent") is None


async def test_upsert_updates_existing_mapping_in_place(fake_db):
    repo = UserMappingRepository(fake_db)
    await repo.upsert(UserMapping(link_group="g1", connector_id="discord", user_id="111", display_name="Alice"))
    await repo.upsert(UserMapping(link_group="g2", connector_id="discord", user_id="111", display_name="Alice2"))

    mapped = await repo.get_mapped_users("g2")
    assert len(mapped) == 1
    assert mapped[0].display_name == "Alice2"


async def test_get_mapped_users_returns_every_connector_in_group(fake_db):
    repo = UserMappingRepository(fake_db)
    await repo.upsert(UserMapping(link_group="g1", connector_id="discord", user_id="111", display_name="Alice"))
    await repo.upsert(UserMapping(link_group="g1", connector_id="irc", user_id="AliceNick", display_name="AliceNick"))

    mapped = await repo.get_mapped_users("g1")
    connector_ids = {m.connector_id for m in mapped}
    assert connector_ids == {"discord", "irc"}


async def test_get_all_for_connector(fake_db):
    repo = UserMappingRepository(fake_db)
    await repo.upsert(UserMapping(link_group="g1", connector_id="irc", user_id="Alice", display_name="Alice"))
    await repo.upsert(UserMapping(link_group="g2", connector_id="irc", user_id="Bob", display_name="Bob"))
    await repo.upsert(UserMapping(link_group="g1", connector_id="discord", user_id="111", display_name="Alice"))

    irc_only = await repo.get_all_for_connector("irc")
    assert {m.user_id for m in irc_only} == {"Alice", "Bob"}
