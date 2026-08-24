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
