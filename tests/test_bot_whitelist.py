from stoat_discord_bridge.storage.bot_whitelist import BotWhitelistEntry, BotWhitelistRepository


async def test_add_and_is_whitelisted(fake_db):
    repo = BotWhitelistRepository(fake_db)
    assert await repo.is_whitelisted("discord", "bot1") is False

    added = await repo.add(BotWhitelistEntry(connector_id="discord", user_id="bot1", label="Webhook Bot"))

    assert added is True
    assert await repo.is_whitelisted("discord", "bot1") is True
    assert await repo.is_whitelisted("stoat", "bot1") is False  # per-connector, not global


async def test_add_is_a_noop_for_an_already_whitelisted_bot(fake_db):
    repo = BotWhitelistRepository(fake_db)
    await repo.add(BotWhitelistEntry(connector_id="discord", user_id="bot1", label="first"))

    added_again = await repo.add(BotWhitelistEntry(connector_id="discord", user_id="bot1", label="second"))

    assert added_again is False
    [entry] = await repo.list_for_connector("discord")
    assert entry.label == "first"  # not overwritten by the repeat add


async def test_remove(fake_db):
    repo = BotWhitelistRepository(fake_db)
    await repo.add(BotWhitelistEntry(connector_id="discord", user_id="bot1"))

    removed = await repo.remove("discord", "bot1")

    assert removed is True
    assert await repo.is_whitelisted("discord", "bot1") is False


async def test_remove_of_an_unwhitelisted_bot_returns_false(fake_db):
    repo = BotWhitelistRepository(fake_db)
    assert await repo.remove("discord", "never-added") is False


async def test_list_for_connector(fake_db):
    repo = BotWhitelistRepository(fake_db)
    await repo.add(BotWhitelistEntry(connector_id="discord", user_id="bot1", label="A"))
    await repo.add(BotWhitelistEntry(connector_id="discord", user_id="bot2", label="B"))
    await repo.add(BotWhitelistEntry(connector_id="stoat", user_id="bot3", label="C"))

    listed = await repo.list_for_connector("discord")

    assert {e.user_id for e in listed} == {"bot1", "bot2"}


async def test_list_for_connector_with_nothing_whitelisted_is_empty(fake_db):
    repo = BotWhitelistRepository(fake_db)
    assert await repo.list_for_connector("discord") == []
