from stoat_discord_bridge.services.mentions import (
    rewrite_channel_mentions,
    rewrite_emoji,
    rewrite_mentions,
    rewrite_role_mentions,
)
from stoat_discord_bridge.storage.channel_mappings import ChannelMapping, ChannelMappingRepository
from stoat_discord_bridge.storage.emoji_mappings import EmojiMappingRepository, EmojiRef
from stoat_discord_bridge.storage.role_mappings import RoleMapping, RoleMappingRepository
from stoat_discord_bridge.storage.user_mappings import UserMapping, UserMappingRepository


_ULID = "01ARZ3NDEKTSV4RRFFQ69G5FAV"


async def _linked_roles(fake_db, *mappings):
    repo = RoleMappingRepository(fake_db)
    for bridge_group, connector_id, role_id, role_name in mappings:
        await repo.upsert(
            RoleMapping(bridge_group=bridge_group, connector_id=connector_id, role_id=role_id, role_name=role_name)
        )
    return repo


async def test_discord_role_mention_rewritten_to_stoat(fake_db):
    repo = await _linked_roles(fake_db, ("g1", "discord", "111", "Mods"), ("g1", "stoat", _ULID, "Moderators"))
    result = await rewrite_role_mentions(
        "ping <@&111>", origin_connector_id="discord", target_connector_id="stoat",
        target_kind="stoat", role_mappings=repo,
    )
    assert result == f"ping <%{_ULID}>"


async def test_stoat_role_mention_rewritten_to_discord(fake_db):
    repo = await _linked_roles(fake_db, ("g1", "stoat", _ULID, "Moderators"), ("g1", "discord", "111", "Mods"))
    result = await rewrite_role_mentions(
        f"ping <%{_ULID}>", origin_connector_id="stoat", target_connector_id="discord",
        target_kind="discord", role_mappings=repo,
    )
    assert result == "ping <@&111>"


async def test_role_mention_to_irc_uses_name(fake_db):
    repo = await _linked_roles(fake_db, ("g1", "discord", "111", "Mods"), ("g1", "irc", "irc-mods", "Mods"))
    result = await rewrite_role_mentions(
        "ping <@&111>", origin_connector_id="discord", target_connector_id="irc",
        target_kind="irc", role_mappings=repo,
    )
    assert result == "ping @Mods"


async def test_unmapped_role_mention_left_untouched(fake_db):
    repo = await _linked_roles(fake_db, ("g1", "discord", "111", "Mods"))
    result = await rewrite_role_mentions(
        "ping <@&111> and <@&222>", origin_connector_id="discord", target_connector_id="stoat",
        target_kind="stoat", role_mappings=repo,
    )
    assert result == "ping <@&111> and <@&222>"


async def _linked_emoji(fake_db, *refs):
    repo = EmojiMappingRepository(fake_db)
    group_id = await repo.try_reserve(EmojiRef(*refs[0]))
    await repo.add_refs(group_id, [EmojiRef(*r) for r in refs[1:]])
    return repo


async def test_discord_custom_emoji_rewritten_to_stoat(fake_db):
    repo = await _linked_emoji(fake_db, ("discord", "989662279748431872", "lmao"), ("stoat", _ULID, "lmao"))
    result = await rewrite_emoji(
        "haha <:lmao:989662279748431872>", origin_connector_id="discord", target_connector_id="stoat",
        target_kind="stoat", emoji_mappings=repo,
    )
    assert result == f"haha :{_ULID}:"


async def test_stoat_custom_emoji_rewritten_to_discord(fake_db):
    repo = await _linked_emoji(fake_db, ("stoat", _ULID, "lmao"), ("discord", "989662279748431872", "lmao"))
    result = await rewrite_emoji(
        f"haha :{_ULID}:", origin_connector_id="stoat", target_connector_id="discord",
        target_kind="discord", emoji_mappings=repo,
    )
    assert result == "haha <:lmao:989662279748431872>"


async def test_custom_emoji_stripped_to_shortcode_on_irc(fake_db):
    repo = await _linked_emoji(fake_db, ("discord", "989662279748431872", "lmao"), ("stoat", _ULID, "lmao"))
    discord_origin = await rewrite_emoji(
        "a <:lmao:989662279748431872> b", origin_connector_id="discord", target_connector_id="irc",
        target_kind="irc", emoji_mappings=repo,
    )
    assert discord_origin == "a :lmao: b"
    stoat_origin = await rewrite_emoji(
        f"a :{_ULID}: b", origin_connector_id="stoat", target_connector_id="irc",
        target_kind="irc", emoji_mappings=repo,
    )
    assert stoat_origin == "a :lmao: b"


async def test_unknown_stoat_emoji_removed_entirely_on_irc(fake_db):
    repo = await _linked_emoji(fake_db, ("discord", "111", "a"), ("stoat", _ULID, "a"))
    other_ulid = "01BX5ZZKBKACTAV9WEVGEMMVRZ"
    result = await rewrite_emoji(
        f"hi :{other_ulid}: there", origin_connector_id="stoat", target_connector_id="irc",
        target_kind="irc", emoji_mappings=repo,
    )
    assert result == "hi  there"


async def test_unmapped_custom_emoji_and_shortcodes_left_untouched(fake_db):
    repo = await _linked_emoji(fake_db, ("discord", "111", "a"), ("stoat", _ULID, "a"))
    result = await rewrite_emoji(
        "<:other:222> and :smile:", origin_connector_id="discord", target_connector_id="stoat",
        target_kind="stoat", emoji_mappings=repo,
    )
    assert result == "<:other:222> and :smile:"


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


async def test_unmapped_mention_left_untouched_when_no_name_known(fake_db):
    repo = UserMappingRepository(fake_db)
    result = await rewrite_mentions(
        "hi <@999> there", origin_connector_id="discord", target_connector_id="stoat",
        target_kind="stoat", user_mappings=repo,
    )
    assert result == "hi <@999> there"


async def test_unmapped_mention_expanded_to_origin_display_name(fake_db):
    # issue #56: user isn't /link-user-linked on the target, so the raw
    # <@id> token is expanded to their display name on the origin instead.
    repo = UserMappingRepository(fake_db)
    result = await rewrite_mentions(
        "hi <@999> there", origin_connector_id="discord", target_connector_id="stoat",
        target_kind="stoat", user_mappings=repo, mentioned_users={"999": "Morning Witch"},
    )
    assert result == "hi @Morning Witch there"


async def test_unmapped_stoat_mention_expanded_to_origin_display_name(fake_db):
    repo = UserMappingRepository(fake_db)
    result = await rewrite_mentions(
        f"hi <@{_ULID}> there", origin_connector_id="stoat", target_connector_id="irc",
        target_kind="irc", user_mappings=repo, mentioned_users={_ULID: "witch"},
    )
    assert result == "hi @witch there"


async def test_mapped_but_no_target_side_expanded_to_origin_name(fake_db):
    # linked discord<->stoat, but relaying to irc which has no entry in this link group
    repo = await _linked(fake_db, ("g1", "discord", "111"), ("g1", "stoat", "s1"))
    result = await rewrite_mentions(
        "hi <@111> there", origin_connector_id="discord", target_connector_id="irc",
        target_kind="irc", user_mappings=repo, mentioned_users={"111": "Alice"},
    )
    assert result == "hi @Alice there"


async def test_linked_mention_still_wins_over_origin_name(fake_db):
    repo = await _linked(fake_db, ("g1", "discord", "111"), ("g1", "stoat", "s1"))
    result = await rewrite_mentions(
        "hi <@111> there", origin_connector_id="discord", target_connector_id="stoat",
        target_kind="stoat", user_mappings=repo, mentioned_users={"111": "Alice"},
    )
    assert result == "hi <@s1> there"


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


async def _linked_channels(fake_db, *mappings):
    repo = ChannelMappingRepository(fake_db)
    for bridge_group, connector_id, channel_id in mappings:
        await repo.upsert(
            ChannelMapping(
                bridge_group=bridge_group, connector_id=connector_id, channel_id=channel_id, channel_name=channel_id
            )
        )
    return repo


async def test_channel_mention_discord_to_stoat(fake_db):
    repo = await _linked_channels(fake_db, ("g1", "discord", "777"), ("g1", "stoat", "01ARZ3NDEKTSV4RRFFQ69G5FAV"))
    result = await rewrite_channel_mentions(
        "see <#777>", origin_connector_id="discord", target_connector_id="stoat",
        target_kind="stoat", channel_mappings=repo,
    )
    assert result == "see <#01ARZ3NDEKTSV4RRFFQ69G5FAV>"


async def test_channel_mention_renders_hash_channel_on_irc(fake_db):
    repo = ChannelMappingRepository(fake_db)
    await repo.upsert(ChannelMapping(bridge_group="g1", connector_id="discord", channel_id="777", channel_name="thread"))
    await repo.upsert(ChannelMapping(bridge_group="g1", connector_id="irc", channel_id="#thread", channel_name="#thread"))
    result = await rewrite_channel_mentions(
        "see <#777>", origin_connector_id="discord", target_connector_id="irc",
        target_kind="irc", channel_mappings=repo,
    )
    assert result == "see #thread"


async def test_channel_mention_unmapped_left_untouched(fake_db):
    repo = ChannelMappingRepository(fake_db)
    result = await rewrite_channel_mentions(
        "see <#777>", origin_connector_id="discord", target_connector_id="stoat",
        target_kind="stoat", channel_mappings=repo,
    )
    assert result == "see <#777>"
