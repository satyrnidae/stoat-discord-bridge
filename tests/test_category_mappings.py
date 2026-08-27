from stoat_discord_bridge.storage.category_mappings import (
    CategoryMapping,
    CategoryMappingRepository,
    ThreadCategoryRepository,
)


async def test_upsert_and_get_bridge_group(fake_db):
    repo = CategoryMappingRepository(fake_db)
    await repo.upsert(
        CategoryMapping(bridge_group="g1", connector_id="discord", category_id="d1", category_name="Team")
    )

    assert await repo.get_bridge_group("discord", "d1") == "g1"
    assert await repo.get_bridge_group("discord", "nonexistent") is None


async def test_upsert_is_idempotent_by_connector_and_category(fake_db):
    repo = CategoryMappingRepository(fake_db)
    await repo.upsert(
        CategoryMapping(bridge_group="g1", connector_id="discord", category_id="d1", category_name="Team")
    )
    await repo.upsert(
        CategoryMapping(bridge_group="g2", connector_id="discord", category_id="d1", category_name="Renamed")
    )

    mapped = await repo.get_mapped_categories("g2")
    assert len(mapped) == 1
    assert mapped[0].category_name == "Renamed"
    assert await repo.get_bridge_group("discord", "d1") == "g2"


async def test_get_mapped_categories_returns_every_connector_in_group(fake_db):
    repo = CategoryMappingRepository(fake_db)
    await repo.upsert(
        CategoryMapping(bridge_group="g1", connector_id="discord", category_id="d1", category_name="Team")
    )
    await repo.upsert(
        CategoryMapping(bridge_group="g1", connector_id="stoat", category_id="s1", category_name="Team")
    )

    mapped = await repo.get_mapped_categories("g1")
    connector_ids = {m.connector_id for m in mapped}
    assert connector_ids == {"discord", "stoat"}


async def test_delete_mapping_removes_just_one_member(fake_db):
    repo = CategoryMappingRepository(fake_db)
    await repo.upsert(
        CategoryMapping(bridge_group="g1", connector_id="discord", category_id="d1", category_name="Team")
    )
    await repo.upsert(
        CategoryMapping(bridge_group="g1", connector_id="stoat", category_id="s1", category_name="Team")
    )

    assert await repo.delete_mapping("stoat", "s1") is True
    assert await repo.delete_mapping("stoat", "s1") is False  # already gone
    mapped = await repo.get_mapped_categories("g1")
    assert {m.connector_id for m in mapped} == {"discord"}


async def test_delete_bridge_group_removes_every_member(fake_db):
    repo = CategoryMappingRepository(fake_db)
    await repo.upsert(
        CategoryMapping(bridge_group="g1", connector_id="discord", category_id="d1", category_name="Team")
    )
    await repo.upsert(
        CategoryMapping(bridge_group="g1", connector_id="stoat", category_id="s1", category_name="Team")
    )

    count = await repo.delete_bridge_group("g1")
    assert count == 2
    assert await repo.get_mapped_categories("g1") == []


async def test_thread_category_mark_and_check(fake_db):
    repo = ThreadCategoryRepository(fake_db)
    assert await repo.is_thread_category("stoat", "cat1") is False

    await repo.mark("stoat", "cat1")
    assert await repo.is_thread_category("stoat", "cat1") is True
    assert await repo.is_thread_category("stoat", "other") is False
    assert await repo.is_thread_category("discord", "cat1") is False


async def test_thread_category_mark_is_idempotent(fake_db):
    repo = ThreadCategoryRepository(fake_db)
    await repo.mark("stoat", "cat1")
    await repo.mark("stoat", "cat1")
    assert await repo.is_thread_category("stoat", "cat1") is True
