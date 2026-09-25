import pytest

from stoat_discord_bridge.admin_commands import AttachmentPreferenceManager, LinkError
from stoat_discord_bridge.storage.attachment_preferences import AttachmentPreferenceRepository


def _manager(fake_db):
    return AttachmentPreferenceManager(AttachmentPreferenceRepository(fake_db))


# ---------------------------------------------------------------- preferred_kind_for


async def test_no_rules_means_no_preference(fake_db):
    manager = _manager(fake_db)
    assert await manager.preferred_kind_for("https://instagram.com/p/abc") is None


async def test_matching_rule_is_returned(fake_db):
    manager = _manager(fake_db)
    await manager.prefer(kind="stoat", url_substring="instagram.com")
    assert await manager.preferred_kind_for("https://www.instagram.com/p/abc") == "stoat"
    assert await manager.preferred_kind_for("https://youtube.com/watch?v=1") is None


async def test_matching_is_case_insensitive(fake_db):
    manager = _manager(fake_db)
    await manager.prefer(kind="Stoat", url_substring="Instagram.COM")
    assert await manager.preferred_kind_for("https://INSTAGRAM.com/p/abc") == "stoat"


async def test_longest_substring_wins(fake_db):
    manager = _manager(fake_db)
    await manager.prefer(kind="stoat", url_substring="instagram.com")
    await manager.prefer(kind="discord", url_substring="instagram.com/reels")
    assert await manager.preferred_kind_for("https://instagram.com/reels/xyz") == "discord"
    assert await manager.preferred_kind_for("https://instagram.com/p/xyz") == "stoat"


async def test_writes_take_effect_immediately_despite_the_cache(fake_db):
    manager = _manager(fake_db)
    assert await manager.preferred_kind_for("https://instagram.com/p/abc") is None
    await manager.prefer(kind="stoat", url_substring="instagram.com")
    assert await manager.preferred_kind_for("https://instagram.com/p/abc") == "stoat"
    await manager.unprefer(url_substring="instagram.com")
    assert await manager.preferred_kind_for("https://instagram.com/p/abc") is None


async def test_lookups_are_cached(fake_db):
    manager = _manager(fake_db)
    await manager.prefer(kind="stoat", url_substring="instagram.com")
    await manager.preferred_kind_for("https://instagram.com/p/abc")
    # A write behind the manager's back isn't seen until the TTL lapses.
    await AttachmentPreferenceRepository(fake_db).remove("instagram.com")
    assert await manager.preferred_kind_for("https://instagram.com/p/abc") == "stoat"


# ---------------------------------------------------------------- prefer / unprefer


async def test_prefer_rejects_an_unknown_kind(fake_db):
    manager = _manager(fake_db)
    with pytest.raises(LinkError, match="discord.*stoat"):
        await manager.prefer(kind="irc", url_substring="instagram.com")


async def test_prefer_rejects_a_blank_substring(fake_db):
    manager = _manager(fake_db)
    with pytest.raises(LinkError, match="empty"):
        await manager.prefer(kind="stoat", url_substring="   ")


async def test_prefer_summaries(fake_db):
    manager = _manager(fake_db)
    assert await manager.prefer(kind="stoat", url_substring="instagram.com") == (
        "Links containing 'instagram.com' will now use stoat's own preview."
    )
    assert await manager.prefer(kind="discord", url_substring="instagram.com") == (
        "Links containing 'instagram.com' will now use discord's own preview (replaced the previous rule)."
    )


async def test_unprefer_of_a_missing_rule_raises(fake_db):
    manager = _manager(fake_db)
    with pytest.raises(LinkError, match="no preference"):
        await manager.unprefer(url_substring="instagram.com")


# ---------------------------------------------------------------- list_preferences


async def test_list_preferences(fake_db):
    manager = _manager(fake_db)
    assert await manager.list_preferences() == "No attachment preferences are set."
    await manager.prefer(kind="stoat", url_substring="instagram.com")
    await manager.prefer(kind="discord", url_substring="youtube.com")
    assert await manager.list_preferences() == (
        "Attachment preferences:\ninstagram.com -> stoat\nyoutube.com -> discord"
    )
