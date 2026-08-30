from stoat_discord_bridge.storage.emoji_mappings import EmojiMappingRepository, EmojiRef


async def test_try_reserve_creates_a_new_group(fake_db):
    repo = EmojiMappingRepository(fake_db)
    group_id = await repo.try_reserve(EmojiRef(connector_id="discord", emoji_id="d1", name="pog"))
    assert group_id is not None
    assert await repo.get_group_id("discord", "d1") == group_id


async def test_try_reserve_rejects_duplicate_ref(fake_db):
    repo = EmojiMappingRepository(fake_db)
    await repo.ensure_indexes()  # the unique index is what makes the second reservation race-proof
    first = await repo.try_reserve(EmojiRef(connector_id="discord", emoji_id="d1", name="pog"))
    second = await repo.try_reserve(EmojiRef(connector_id="discord", emoji_id="d1", name="pog"))
    assert first is not None
    assert second is None


async def test_add_refs_and_find_equivalent(fake_db):
    repo = EmojiMappingRepository(fake_db)
    group_id = await repo.try_reserve(EmojiRef(connector_id="discord", emoji_id="d1", name="pog"))
    await repo.add_refs(group_id, [EmojiRef(connector_id="stoat", emoji_id="s1", name="pog")])

    assert await repo.find_equivalent("discord", "d1", "stoat") == "s1"
    assert await repo.find_equivalent("stoat", "s1", "discord") == "d1"
    assert await repo.find_equivalent("discord", "d1", "irc") is None  # never mirrored there


async def test_release_drops_the_group(fake_db):
    repo = EmojiMappingRepository(fake_db)
    group_id = await repo.try_reserve(EmojiRef(connector_id="discord", emoji_id="d1", name="pog"))
    await repo.release(group_id)
    assert await repo.get_group_id("discord", "d1") is None


async def test_forget_removes_only_that_connectors_ref(fake_db):
    repo = EmojiMappingRepository(fake_db)
    group_id = await repo.try_reserve(EmojiRef(connector_id="discord", emoji_id="d1", name="pog"))
    await repo.add_refs(group_id, [EmojiRef(connector_id="stoat", emoji_id="s1", name="pog")])

    await repo.forget("discord", "d1")
    assert await repo.find_equivalent("discord", "d1", "stoat") is None  # discord's ref is gone
    assert await repo.get_group_id("stoat", "s1") == group_id  # stoat's ref survives


async def test_forget_last_ref_deletes_the_whole_group(fake_db):
    repo = EmojiMappingRepository(fake_db)
    group_id = await repo.try_reserve(EmojiRef(connector_id="discord", emoji_id="d1", name="pog"))
    await repo.forget("discord", "d1")
    assert await repo.get_group_id("discord", "d1") is None
    assert fake_db["emoji_mappings"].docs == {}


async def test_get_refs_returns_the_groups_refs(fake_db):
    repo = EmojiMappingRepository(fake_db)
    group_id = await repo.try_reserve(EmojiRef(connector_id="discord", emoji_id="d1", name="pog"))
    await repo.add_refs(group_id, [EmojiRef(connector_id="stoat", emoji_id="s1", name="pog")])

    refs = await repo.get_refs(group_id)
    assert {(r.connector_id, r.emoji_id) for r in refs} == {("discord", "d1"), ("stoat", "s1")}


async def test_get_all_groups(fake_db):
    repo = EmojiMappingRepository(fake_db)
    g1 = await repo.try_reserve(EmojiRef(connector_id="discord", emoji_id="d1", name="a"))
    g2 = await repo.try_reserve(EmojiRef(connector_id="discord", emoji_id="d2", name="b"))

    groups = await repo.get_all_groups()
    assert set(groups) == {g1, g2}


async def test_delete_ref_pulls_one_without_group_cleanup(fake_db):
    repo = EmojiMappingRepository(fake_db)
    group_id = await repo.try_reserve(EmojiRef(connector_id="discord", emoji_id="d1", name="pog"))

    await repo.delete_ref("discord", "d1")
    assert await repo.get_group_id("discord", "d1") is None
    assert await repo.get_refs(group_id) == []  # group doc still exists, just empty


async def test_delete_group_drops_it_and_reports_ref_count(fake_db):
    repo = EmojiMappingRepository(fake_db)
    group_id = await repo.try_reserve(EmojiRef(connector_id="discord", emoji_id="d1", name="pog"))
    await repo.add_refs(group_id, [EmojiRef(connector_id="stoat", emoji_id="s1", name="pog")])

    assert await repo.delete_group(group_id) == 2
    assert await repo.get_group_id("stoat", "s1") is None
