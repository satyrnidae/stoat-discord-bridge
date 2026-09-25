from stoat_discord_bridge.storage.attachment_preferences import (
    AttachmentPreference,
    AttachmentPreferenceRepository,
)


async def test_set_and_list(fake_db):
    repo = AttachmentPreferenceRepository(fake_db)
    assert await repo.list_all() == []

    created = await repo.set("instagram.com", "stoat")

    assert created is True
    assert await repo.list_all() == [AttachmentPreference(url_substring="instagram.com", preferred_kind="stoat")]


async def test_set_overwrites_an_existing_rule(fake_db):
    repo = AttachmentPreferenceRepository(fake_db)
    await repo.set("instagram.com", "stoat")

    created = await repo.set("instagram.com", "discord")

    assert created is False
    assert await repo.list_all() == [AttachmentPreference(url_substring="instagram.com", preferred_kind="discord")]


async def test_remove(fake_db):
    repo = AttachmentPreferenceRepository(fake_db)
    await repo.set("instagram.com", "stoat")

    assert await repo.remove("instagram.com") is True
    assert await repo.list_all() == []


async def test_remove_of_a_missing_rule_returns_false(fake_db):
    repo = AttachmentPreferenceRepository(fake_db)
    assert await repo.remove("never-added") is False
