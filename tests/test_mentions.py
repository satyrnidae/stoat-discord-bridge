from stoat_discord_bridge.services.mentions import rewrite_mentions
from stoat_discord_bridge.storage.user_mappings import UserMapping, UserMappingRepository


async def _linked(fake_db, *mappings):
    repo = UserMappingRepository(fake_db)
    for link_group, connector_id, user_id in mappings:
        await repo.upsert(UserMapping(link_group=link_group, connector_id=connector_id, user_id=user_id, display_name=user_id))
    return repo


async def test_discord_mention_rewritten_to_stoat(fake_db):
    repo = await _linked(fake_db, ("g1", "discord", "111"), ("g1", "stoat", "01ARZ3NDEKTSV4RRFFQ69G5FAV"))
    result = await rewrite_mentions(
        "hi <@111> there", origin_connector_id="discord", target_connector_id="stoat",
        target_kind="stoat", user_mappings=repo,
    )
    assert result == "hi <@01ARZ3NDEKTSV4RRFFQ69G5FAV> there"


async def test_discord_mention_with_nickname_bang_form(fake_db):
    repo = await _linked(fake_db, ("g1", "discord", "111"), ("g1", "irc", "Alice"))
    result = await rewrite_mentions(
        "hi <@!111> there", origin_connector_id="discord", target_connector_id="irc",
        target_kind="irc", user_mappings=repo,
    )
    assert result == "hi Alice there"


async def test_stoat_mention_rewritten_to_discord(fake_db):
    repo = await _linked(fake_db, ("g1", "stoat", "01ARZ3NDEKTSV4RRFFQ69G5FAV"), ("g1", "discord", "111"))
    result = await rewrite_mentions(
        "hi <@01ARZ3NDEKTSV4RRFFQ69G5FAV> there", origin_connector_id="stoat", target_connector_id="discord",
        target_kind="discord", user_mappings=repo,
    )
    assert result == "hi <@111> there"


async def test_unmapped_mention_left_untouched(fake_db):
    repo = UserMappingRepository(fake_db)
    result = await rewrite_mentions(
        "hi <@999> there", origin_connector_id="discord", target_connector_id="stoat",
        target_kind="stoat", user_mappings=repo,
    )
    assert result == "hi <@999> there"


async def test_mapped_but_no_target_side_left_untouched(fake_db):
    # linked discord<->stoat, but relaying to irc which has no entry in this link group
    repo = await _linked(fake_db, ("g1", "discord", "111"), ("g1", "stoat", "s1"))
    result = await rewrite_mentions(
        "hi <@111> there", origin_connector_id="discord", target_connector_id="irc",
        target_kind="irc", user_mappings=repo,
    )
    assert result == "hi <@111> there"


async def test_irc_plain_nick_mention_rewritten_to_discord(fake_db):
    repo = await _linked(fake_db, ("g1", "irc", "Alice"), ("g1", "discord", "111"))
    result = await rewrite_mentions(
        "Alice did you see this", origin_connector_id="irc", target_connector_id="discord",
        target_kind="discord", user_mappings=repo,
    )
    assert result == "<@111> did you see this"


async def test_irc_nick_mention_is_word_boundary_matched(fake_db):
    # "Alice" should NOT match inside "Alicent" - word-boundary, not substring
    repo = await _linked(fake_db, ("g1", "irc", "Alice"), ("g1", "discord", "111"))
    result = await rewrite_mentions(
        "Alicent said hi to Alice", origin_connector_id="irc", target_connector_id="discord",
        target_kind="discord", user_mappings=repo,
    )
    assert result == "Alicent said hi to <@111>"


async def test_irc_target_renders_plain_nick(fake_db):
    repo = await _linked(fake_db, ("g1", "discord", "111"), ("g1", "irc", "Bob"))
    result = await rewrite_mentions(
        "hey <@111>", origin_connector_id="discord", target_connector_id="irc",
        target_kind="irc", user_mappings=repo,
    )
    assert result == "hey Bob"


async def test_multiple_mentions_in_one_message(fake_db):
    repo = await _linked(
        fake_db,
        ("g1", "discord", "111"), ("g1", "stoat", "s1"),
        ("g2", "discord", "222"), ("g2", "stoat", "s2"),
    )
    result = await rewrite_mentions(
        "<@111> and <@222> both said hi", origin_connector_id="discord", target_connector_id="stoat",
        target_kind="stoat", user_mappings=repo,
    )
    assert result == "<@s1> and <@s2> both said hi"
