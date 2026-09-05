import pytest

import dataclasses

from stoat_discord_bridge.admin_commands import ConnectorInfo, EmoteLinker, LinkError
from stoat_discord_bridge.models import CustomEmoji
from stoat_discord_bridge.storage.emoji_mappings import EmojiMappingRepository


# ---------------------------------------------------------------- EmoteLinker.link_emote


async def test_link_emote_creates_a_new_group(fake_db, connectors):
    linker = EmoteLinker(EmojiMappingRepository(fake_db), connectors)
    summary = await linker.link_emote(local_connector="stoat", local_id="s1", source="discord", source_id="d1")
    assert "Linked Discord emote 'd1' to Stoat emote 's1'" in summary


async def test_link_emote_accepts_shortcode_and_custom_emoji_tokens(fake_db):
    async def d_by_name(token):
        return {"blob": "d1"}.get(token)

    connectors = {
        "discord": ConnectorInfo(id="discord", label="Discord", resolve_emoji_id_by_name=d_by_name),
        "stoat": ConnectorInfo(id="stoat", label="Stoat"),
    }
    linker = EmoteLinker(EmojiMappingRepository(fake_db), connectors)

    summary = await linker.link_emote(local_connector="stoat", local_id="s1", source="discord", source_id=":blob:")
    assert "Linked Discord emote 'd1'" in summary

    # a full <:name:id> reference reduces to the bare id
    summary = await linker.link_emote(
        local_connector="stoat", local_id="s2", source="discord", source_id="<:blob:d1>"
    )
    assert "Linked Discord emote 'd1'" in summary


async def test_link_emote_unknown_source_raises(fake_db, connectors):
    linker = EmoteLinker(EmojiMappingRepository(fake_db), connectors)
    with pytest.raises(LinkError, match="isn't a known connector"):
        await linker.link_emote(local_connector="stoat", local_id="s1", source="nope", source_id="d1")


async def test_link_emote_to_itself_raises(fake_db, connectors):
    linker = EmoteLinker(EmojiMappingRepository(fake_db), connectors)
    with pytest.raises(LinkError, match="itself"):
        await linker.link_emote(local_connector="discord", local_id="d1", source="discord", source_id="d1")


async def test_link_emote_merges_third_connector_into_existing_group(fake_db, connectors):
    emoji_mappings = EmojiMappingRepository(fake_db)
    linker = EmoteLinker(emoji_mappings, connectors)
    await linker.link_emote(local_connector="stoat", local_id="s1", source="discord", source_id="d1")
    await linker.link_emote(local_connector="irc", local_id="i1", source="discord", source_id="d1")

    assert await emoji_mappings.find_equivalent("stoat", "s1", "irc") == "i1"


async def test_link_emote_conflicting_groups_raises(fake_db, connectors):
    linker = EmoteLinker(EmojiMappingRepository(fake_db), connectors)
    await linker.link_emote(local_connector="stoat", local_id="s1", source="discord", source_id="d1")
    await linker.link_emote(local_connector="irc", local_id="i1", source="discord", source_id="d2")

    with pytest.raises(LinkError, match="different mapping groups"):
        await linker.link_emote(local_connector="irc", local_id="i1", source="discord", source_id="d1")


# ---------------------------------------------------------------- EmoteLinker: unlink / mirror / linked


@pytest.fixture
def emote_connectors():
    created: dict[str, list[CustomEmoji]] = {"discord": [], "stoat": []}

    def _resolve_emoji(conn):
        async def _inner(emoji_id):
            if emoji_id == f"{conn[0]}src":
                return CustomEmoji(native_id=emoji_id, name="blob", image_url="http://x/blob.png", animated=False)
            return None

        return _inner

    def _ensure_emoji(conn):
        async def _inner(emoji: CustomEmoji):
            new = CustomEmoji(
                native_id=f"{conn[0]}new", name=emoji.name, image_url=emoji.image_url, animated=emoji.animated
            )
            created[conn].append(new)
            return new

        return _inner

    return {
        "discord": ConnectorInfo(
            id="discord", label="Discord", resolve_emoji=_resolve_emoji("discord"), ensure_emoji=_ensure_emoji("discord")
        ),
        "stoat": ConnectorInfo(
            id="stoat", label="Stoat", resolve_emoji=_resolve_emoji("stoat"), ensure_emoji=_ensure_emoji("stoat")
        ),
    }


async def test_unlink_emote_all_dissolves_the_group(fake_db, connectors):
    emoji_mappings = EmojiMappingRepository(fake_db)
    linker = EmoteLinker(emoji_mappings, connectors)
    await linker.link_emote(local_connector="stoat", local_id="s1", source="discord", source_id="d1")

    summary = await linker.unlink_emote(local_connector="discord", local_emote="d1", destination=None)

    assert "entire mapping group" in summary
    assert await emoji_mappings.get_group_id("stoat", "s1") is None


async def test_unlink_emote_one_member_strands_lone_survivor_so_dissolves(fake_db, connectors):
    emoji_mappings = EmojiMappingRepository(fake_db)
    linker = EmoteLinker(emoji_mappings, connectors)
    await linker.link_emote(local_connector="stoat", local_id="s1", source="discord", source_id="d1")

    await linker.unlink_emote(local_connector="discord", local_emote="d1", destination="stoat")

    assert await emoji_mappings.get_group_id("discord", "d1") is None


async def test_unlink_emote_unlinked_raises(fake_db, connectors):
    linker = EmoteLinker(EmojiMappingRepository(fake_db), connectors)
    with pytest.raises(LinkError, match="isn't linked"):
        await linker.unlink_emote(local_connector="discord", local_emote="d1", destination=None)


async def test_list_linked_emotes_no_argument_lists_every_group(fake_db, connectors):
    linker = EmoteLinker(EmojiMappingRepository(fake_db), connectors)
    await linker.link_emote(local_connector="stoat", local_id="s1", source="discord", source_id="d1")

    summary = await linker.list_linked_emotes(local_connector="discord")

    assert summary.startswith("Linked emotes:")
    assert "Discord: d1" in summary and "Stoat: s1" in summary


async def test_mirror_emote_recreates_and_links(fake_db, emote_connectors):
    emoji_mappings = EmojiMappingRepository(fake_db)
    linker = EmoteLinker(emoji_mappings, emote_connectors)

    summary = await linker.mirror_emote(local_connector="discord", local_emote="dsrc", destination="stoat")

    assert "Linked" in summary
    assert await emoji_mappings.find_equivalent("discord", "dsrc", "stoat") == "snew"


async def test_mirror_emote_new_name_renames_the_recreated_copy(fake_db, emote_connectors):
    # issue #44: the recreated emoji takes `new_name`, not the source name.
    emoji_mappings = EmojiMappingRepository(fake_db)
    created: list[CustomEmoji] = []

    async def _ensure(emoji: CustomEmoji):
        new = CustomEmoji(native_id="snew", name=emoji.name, image_url=emoji.image_url, animated=emoji.animated)
        created.append(new)
        return new

    emote_connectors["stoat"] = dataclasses.replace(emote_connectors["stoat"], ensure_emoji=_ensure)
    linker = EmoteLinker(emoji_mappings, emote_connectors)

    await linker.mirror_emote(
        local_connector="discord", local_emote="dsrc", destination="stoat", new_name="blobcat"
    )

    assert [e.name for e in created] == ["blobcat"]


async def test_mirror_emote_new_name_drives_the_same_named_match_lookup(fake_db, emote_connectors):
    emoji_mappings = EmojiMappingRepository(fake_db)
    looked_up: list[str] = []

    async def s_by_name(token):
        looked_up.append(token)
        return "s-existing" if token == "blobcat" else None

    emote_connectors["stoat"] = dataclasses.replace(
        emote_connectors["stoat"], resolve_emoji_id_by_name=s_by_name
    )
    linker = EmoteLinker(emoji_mappings, emote_connectors)

    summary = await linker.mirror_emote(
        local_connector="discord", local_emote="dsrc", destination="stoat", new_name="blobcat"
    )

    assert looked_up[0] == "blobcat"  # the destination match is keyed off new_name, not "blob"
    assert "Linked" in summary
    assert await emoji_mappings.find_equivalent("discord", "dsrc", "stoat") == "s-existing"


async def test_mirror_emote_links_to_an_existing_same_named_emote_instead_of_duplicating(fake_db, emote_connectors):
    emoji_mappings = EmojiMappingRepository(fake_db)

    async def s_by_name(token):
        return {"blob": "s-existing"}.get(token)

    async def d_name(emoji_id):
        return "blob"

    emote_connectors["discord"] = dataclasses.replace(emote_connectors["discord"], resolve_emoji_name=d_name)
    emote_connectors["stoat"] = dataclasses.replace(
        emote_connectors["stoat"], resolve_emoji_id_by_name=s_by_name
    )
    linker = EmoteLinker(emoji_mappings, emote_connectors)

    summary = await linker.mirror_emote(local_connector="discord", local_emote="dsrc", destination="stoat")

    assert "Linked" in summary
    assert await emoji_mappings.find_equivalent("discord", "dsrc", "stoat") == "s-existing"


async def test_mirror_emote_already_synced_is_skipped(fake_db, emote_connectors):
    emoji_mappings = EmojiMappingRepository(fake_db)
    linker = EmoteLinker(emoji_mappings, emote_connectors)
    await linker.mirror_emote(local_connector="discord", local_emote="dsrc", destination="stoat")

    summary = await linker.mirror_emote(local_connector="discord", local_emote="dsrc", destination="stoat")

    assert "already synced" in summary


async def test_mirror_emote_missing_source_reports(fake_db, emote_connectors):
    linker = EmoteLinker(EmojiMappingRepository(fake_db), emote_connectors)
    summary = await linker.mirror_emote(local_connector="discord", local_emote="nope", destination="stoat")
    assert "not found" in summary


async def test_mirror_emote_from_recreates_the_remote_emote_locally(fake_db, emote_connectors):
    emoji_mappings = EmojiMappingRepository(fake_db)
    linker = EmoteLinker(emoji_mappings, emote_connectors)

    # run "on stoat", pulling discord's `dsrc` in
    summary = await linker.mirror_emote_from(local_connector="stoat", source="discord", source_emote="dsrc")

    assert "Linked" in summary
    assert await emoji_mappings.find_equivalent("discord", "dsrc", "stoat") == "snew"


async def test_mirror_emote_from_own_connector_raises(fake_db, emote_connectors):
    linker = EmoteLinker(EmojiMappingRepository(fake_db), emote_connectors)
    with pytest.raises(LinkError, match="from a connector to itself"):
        await linker.mirror_emote_from(local_connector="discord", source="discord", source_emote="dsrc")