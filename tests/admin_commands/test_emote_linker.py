import pytest

import dataclasses

from stoat_discord_bridge.admin_commands import ConnectorInfo, EmoteLinker, LinkedMember, LinkError, NothingLinkedError
from stoat_discord_bridge.models import CustomEmoji, EmojiCapacity
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


# ---------------------------------------------------------------- EmoteLinker.unlink_emote (all, issue #181)


async def test_unlink_emote_all_all_dissolves_only_the_local_connectors_groups(fake_db, connectors):
    emoji_mappings = EmojiMappingRepository(fake_db)
    linker = EmoteLinker(emoji_mappings, connectors)
    await linker.link_emote(local_connector="stoat", local_id="s1", source="discord", source_id="d1")
    await linker.link_emote(local_connector="stoat", local_id="s2", source="discord", source_id="d2")
    await linker.link_emote(local_connector="irc", local_id="i3", source="discord", source_id="d3")

    summary = await linker.unlink_emote(local_connector="stoat", local_emote="ALL", destination="all")

    assert summary.splitlines()[0] == "Dissolved 2 mapping group(s) on Stoat:"
    for connector_id, emoji_id in (("stoat", "s1"), ("discord", "d1"), ("stoat", "s2"), ("discord", "d2")):
        assert await emoji_mappings.get_group_id(connector_id, emoji_id) is None
    assert await emoji_mappings.get_group_id("discord", "d3") is not None


async def test_unlink_emote_all_with_service_kicks_it_and_dissolves_a_lone_survivor(fake_db, connectors):
    emoji_mappings = EmojiMappingRepository(fake_db)
    linker = EmoteLinker(emoji_mappings, connectors)
    # 3-way group: kicking IRC leaves Stoat+Discord linked
    await linker.link_emote(local_connector="stoat", local_id="s1", source="discord", source_id="d1")
    await linker.link_emote(local_connector="irc", local_id="i1", source="discord", source_id="d1")
    # 2-way group: kicking IRC would strand Stoat alone, so it's dissolved
    await linker.link_emote(local_connector="stoat", local_id="s2", source="irc", source_id="i2")
    # no IRC member: skipped
    await linker.link_emote(local_connector="stoat", local_id="s3", source="discord", source_id="d3")

    summary = await linker.unlink_emote(local_connector="stoat", local_emote="all", destination="irc")

    assert "'s1': unlinked IRC emote 'i1'" in summary and "'s3'" not in summary
    assert await emoji_mappings.get_group_id("irc", "i1") is None
    assert await emoji_mappings.find_equivalent("stoat", "s1", "discord") == "d1"
    assert await emoji_mappings.get_group_id("stoat", "s2") is None
    assert await emoji_mappings.get_group_id("stoat", "s3") is not None


async def test_unlink_emote_all_with_nothing_linked_raises_nothing_linked_error(fake_db, connectors):
    linker = EmoteLinker(EmojiMappingRepository(fake_db), connectors)
    with pytest.raises(NothingLinkedError, match="no emotes on Stoat are linked"):
        await linker.unlink_emote(local_connector="stoat", local_emote="all", destination="all")


async def test_unlink_emote_all_without_service_raises(fake_db, connectors):
    linker = EmoteLinker(EmojiMappingRepository(fake_db), connectors)
    await linker.link_emote(local_connector="stoat", local_id="s1", source="discord", source_id="d1")
    with pytest.raises(LinkError, match="explicit service"):
        await linker.unlink_emote(local_connector="stoat", local_emote="all", destination=None)


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


# ---------------------------------------------------------------- EmoteLinker.mirror_emote: emoji-slot capacity pre-check (issue #157)


async def test_mirror_emote_skips_the_create_when_the_matching_slot_pool_is_full(fake_db, emote_connectors):
    async def capacity():
        return EmojiCapacity(free_static=0, free_animated=5)

    emote_connectors["stoat"] = dataclasses.replace(emote_connectors["stoat"], emoji_capacity=capacity)
    emoji_mappings = EmojiMappingRepository(fake_db)
    linker = EmoteLinker(emoji_mappings, emote_connectors)

    summary = await linker.mirror_emote(local_connector="discord", local_emote="dsrc", destination="stoat")

    assert "no static emoji slots left" in summary
    assert await emoji_mappings.find_equivalent("discord", "dsrc", "stoat") is None


async def test_mirror_emote_creates_when_the_matching_slot_pool_has_room(fake_db, emote_connectors):
    async def capacity():
        return EmojiCapacity(free_static=1, free_animated=0)

    emote_connectors["stoat"] = dataclasses.replace(emote_connectors["stoat"], emoji_capacity=capacity)
    emoji_mappings = EmojiMappingRepository(fake_db)
    linker = EmoteLinker(emoji_mappings, emote_connectors)

    summary = await linker.mirror_emote(local_connector="discord", local_emote="dsrc", destination="stoat")

    assert "Linked" in summary
    assert await emoji_mappings.find_equivalent("discord", "dsrc", "stoat") == "snew"


async def test_mirror_emote_checks_the_animated_pool_for_an_animated_source_emoji(fake_db, emote_connectors):
    async def resolve_animated(emoji_id):
        return CustomEmoji(native_id=emoji_id, name="blob", image_url="http://x/blob.gif", animated=True)

    async def capacity():
        # static pool is full, but the source emoji is animated - the
        # animated pool (which has room) is the one that should be checked.
        return EmojiCapacity(free_static=0, free_animated=1)

    emote_connectors["discord"] = dataclasses.replace(emote_connectors["discord"], resolve_emoji=resolve_animated)
    emote_connectors["stoat"] = dataclasses.replace(emote_connectors["stoat"], emoji_capacity=capacity)
    emoji_mappings = EmojiMappingRepository(fake_db)
    linker = EmoteLinker(emoji_mappings, emote_connectors)

    summary = await linker.mirror_emote(local_connector="discord", local_emote="dsrc", destination="stoat")

    assert "Linked" in summary


async def test_mirror_emote_create_proceeds_when_capacity_is_unknown(fake_db, emote_connectors):
    async def capacity():
        return None

    emote_connectors["stoat"] = dataclasses.replace(emote_connectors["stoat"], emoji_capacity=capacity)
    emoji_mappings = EmojiMappingRepository(fake_db)
    linker = EmoteLinker(emoji_mappings, emote_connectors)

    summary = await linker.mirror_emote(local_connector="discord", local_emote="dsrc", destination="stoat")

    assert "Linked" in summary


async def test_mirror_emote_create_proceeds_when_the_capacity_hook_raises(fake_db, emote_connectors):
    async def capacity():
        raise RuntimeError("guild not cached yet")

    emote_connectors["stoat"] = dataclasses.replace(emote_connectors["stoat"], emoji_capacity=capacity)
    emoji_mappings = EmojiMappingRepository(fake_db)
    linker = EmoteLinker(emoji_mappings, emote_connectors)

    summary = await linker.mirror_emote(local_connector="discord", local_emote="dsrc", destination="stoat")

    assert "Linked" in summary


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


async def test_mirror_emote_does_not_reuse_a_same_named_emote_linked_elsewhere(fake_db, emote_connectors):
    # issue #182: Discord allows duplicate emoji names, so the name match can
    # hit an emote already linked to a different one - create a fresh copy
    # instead of joining that unrelated group.
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
    await linker.link_emote(local_connector="stoat", local_id="s-existing", source="discord", source_id="d-other")
    other_group = await emoji_mappings.get_group_id("stoat", "s-existing")

    summary = await linker.mirror_emote(local_connector="discord", local_emote="dsrc", destination="stoat")

    assert "Linked" in summary
    assert await emoji_mappings.find_equivalent("discord", "dsrc", "stoat") == "snew"
    assert await emoji_mappings.get_group_id("discord", "dsrc") != other_group
    assert {r.emoji_id for r in await emoji_mappings.get_refs(other_group)} == {"s-existing", "d-other"}


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


# ---------------------------------------------------------------- EmoteLinker.describe_group


async def test_describe_group_returns_none_for_an_unlinked_emote(fake_db, connectors):
    linker = EmoteLinker(EmojiMappingRepository(fake_db), connectors)
    assert await linker.describe_group(local_connector="stoat", local_id="s1") is None


async def test_describe_group_returns_the_group_id_and_members(fake_db, connectors):
    emoji_mappings = EmojiMappingRepository(fake_db)
    linker = EmoteLinker(emoji_mappings, connectors)
    await linker.link_emote(local_connector="stoat", local_id="s1", source="discord", source_id="d1")

    result = await linker.describe_group(local_connector="stoat", local_id="s1")

    assert result is not None
    group_id, members = result
    assert group_id == await emoji_mappings.get_group_id("stoat", "s1")
    assert members == [
        LinkedMember(connector_id="discord", label="Discord", entity_id="d1", name="d1"),
        LinkedMember(connector_id="stoat", label="Stoat", entity_id="s1", name="s1"),
    ]


# ---------------------------------------------------------------- entity-level `all` (issue #123)


def _all_emote_connectors():
    created: list[CustomEmoji] = []

    async def resolve_emoji(emoji_id):
        return {
            "d1": CustomEmoji(native_id="d1", name="blob", image_url="http://x/blob.png", animated=False),
            "d2": CustomEmoji(native_id="d2", name="party", image_url="http://x/party.png", animated=False),
        }.get(emoji_id)

    async def ensure_emoji(emoji):
        new = CustomEmoji(
            native_id=f"stoat_{emoji.name}", name=emoji.name, image_url=emoji.image_url, animated=emoji.animated
        )
        created.append(new)
        return new

    async def list_emotes():
        return [("d1", "blob"), ("d2", "party")]

    async def resolve_emoji_name(emoji_id):
        return {"d1": "blob", "d2": "party"}.get(emoji_id)

    connectors = {
        "discord": ConnectorInfo(
            id="discord",
            label="Discord",
            resolve_emoji=resolve_emoji,
            resolve_emoji_name=resolve_emoji_name,
            list_emotes=list_emotes,
        ),
        "stoat": ConnectorInfo(id="stoat", label="Stoat", ensure_emoji=ensure_emoji),
    }
    return connectors, created


async def test_mirror_emote_to_all_mirrors_every_local_emote(fake_db):
    connectors, created = _all_emote_connectors()
    linker = EmoteLinker(EmojiMappingRepository(fake_db), connectors)

    summary = await linker.mirror_emote(local_connector="discord", local_emote="all", destination="stoat")

    assert [e.name for e in created] == ["blob", "party"]
    lines = summary.splitlines()
    assert len(lines) == 2
    assert all("Linked" in line for line in lines)


async def test_mirror_emote_to_all_without_list_emotes_raises(fake_db, connectors):
    linker = EmoteLinker(EmojiMappingRepository(fake_db), connectors)
    with pytest.raises(LinkError, match="doesn't support listing emotes"):
        await linker.mirror_emote(local_connector="irc", local_emote="all", destination="stoat")


async def test_mirror_emote_to_all_rejects_a_new_name(fake_db):
    connectors, _created = _all_emote_connectors()
    linker = EmoteLinker(EmojiMappingRepository(fake_db), connectors)

    with pytest.raises(LinkError, match="all.*together with a new name"):
        await linker.mirror_emote(
            local_connector="discord", local_emote="ALL", destination="stoat", new_name="Renamed"
        )


async def test_mirror_emote_from_all_pulls_in_every_source_emote(fake_db):
    created: list[CustomEmoji] = []

    async def resolve_emoji(emoji_id):
        return {
            "s1": CustomEmoji(native_id="s1", name="blob", image_url="http://x/blob.png", animated=False),
            "s2": CustomEmoji(native_id="s2", name="party", image_url="http://x/party.png", animated=False),
        }.get(emoji_id)

    async def ensure_emoji(emoji):
        new = CustomEmoji(
            native_id=f"discord_{emoji.name}", name=emoji.name, image_url=emoji.image_url, animated=emoji.animated
        )
        created.append(new)
        return new

    async def list_emotes():
        return [("s1", "blob"), ("s2", "party")]

    async def resolve_emoji_name(emoji_id):
        return {"s1": "blob", "s2": "party"}.get(emoji_id)

    connectors = {
        "stoat": ConnectorInfo(
            id="stoat",
            label="Stoat",
            resolve_emoji=resolve_emoji,
            resolve_emoji_name=resolve_emoji_name,
            list_emotes=list_emotes,
        ),
        "discord": ConnectorInfo(id="discord", label="Discord", ensure_emoji=ensure_emoji),
    }
    linker = EmoteLinker(EmojiMappingRepository(fake_db), connectors)

    summary = await linker.mirror_emote_from(local_connector="discord", source="stoat", source_emote="all")

    assert [e.name for e in created] == ["blob", "party"]
    lines = summary.splitlines()
    assert len(lines) == 2
    assert all("Linked" in line for line in lines)
