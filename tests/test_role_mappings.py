from stoat_discord_bridge.storage.role_mappings import RoleMapping, RoleMappingRepository


async def test_upsert_and_get_bridge_group(fake_db):
    repo = RoleMappingRepository(fake_db)
    await repo.upsert(RoleMapping(bridge_group="g1", connector_id="discord", role_id="d1", role_name="Mods"))
    assert await repo.get_bridge_group("discord", "d1") == "g1"
    assert await repo.get_bridge_group("discord", "nope") is None


async def test_upsert_is_idempotent_by_connector_and_role(fake_db):
    repo = RoleMappingRepository(fake_db)
    await repo.upsert(RoleMapping(bridge_group="g1", connector_id="discord", role_id="d1", role_name="Mods"))
    await repo.upsert(RoleMapping(bridge_group="g1", connector_id="discord", role_id="d1", role_name="Moderators"))
    mapped = await repo.get_mapped_roles("g1")
    assert len(mapped) == 1
    assert mapped[0].role_name == "Moderators"


async def test_upsert_replaces_stale_same_connector_entry_in_group(fake_db):
    repo = RoleMappingRepository(fake_db)
    await repo.upsert(RoleMapping(bridge_group="g1", connector_id="discord", role_id="wrong", role_name="x"))
    await repo.upsert(RoleMapping(bridge_group="g1", connector_id="discord", role_id="right", role_name="x"))
    mapped = await repo.get_mapped_roles("g1")
    assert [m.role_id for m in mapped] == ["right"]


async def test_find_linked_role_id(fake_db):
    repo = RoleMappingRepository(fake_db)
    await repo.upsert(RoleMapping(bridge_group="g1", connector_id="discord", role_id="d1", role_name="Mods"))
    await repo.upsert(RoleMapping(bridge_group="g1", connector_id="stoat", role_id="s1", role_name="Mods"))
    assert await repo.find_linked_role_id("discord", "d1", "stoat") == "s1"
    assert await repo.find_linked_role_id("discord", "d1", "irc") is None
    assert await repo.find_linked_role_id("discord", "unlinked", "stoat") is None


async def test_delete_mapping_and_delete_bridge_group(fake_db):
    repo = RoleMappingRepository(fake_db)
    await repo.upsert(RoleMapping(bridge_group="g1", connector_id="discord", role_id="d1", role_name="Mods"))
    await repo.upsert(RoleMapping(bridge_group="g1", connector_id="stoat", role_id="s1", role_name="Mods"))
    assert await repo.delete_mapping("discord", "d1") is True
    assert await repo.delete_mapping("discord", "d1") is False
    assert await repo.delete_bridge_group("g1") == 1


async def test_get_all_and_get_all_for_connector(fake_db):
    repo = RoleMappingRepository(fake_db)
    await repo.upsert(RoleMapping(bridge_group="g1", connector_id="discord", role_id="d1", role_name="Mods"))
    await repo.upsert(RoleMapping(bridge_group="g2", connector_id="stoat", role_id="s9", role_name="VIP"))
    assert len(await repo.get_all()) == 2
    assert [m.role_id for m in await repo.get_all_for_connector("stoat")] == ["s9"]
