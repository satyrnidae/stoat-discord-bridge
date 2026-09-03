import dataclasses

import pytest

from stoat_discord_bridge.admin_commands import (
    ChannelLinker,
    ConnectorInfo,
    EmoteLinker,
    LinkError,
    UserLinker,
)
from stoat_discord_bridge.models import ChannelMetadata, CustomEmoji
from stoat_discord_bridge.storage.category_mappings import CategoryMapping, CategoryMappingRepository
from stoat_discord_bridge.storage.channel_mappings import ChannelMapping, ChannelMappingRepository
from stoat_discord_bridge.storage.emoji_mappings import EmojiMappingRepository
from stoat_discord_bridge.storage.user_mappings import UserMappingRepository


@pytest.fixture
def connectors():
    return {
        "discord": ConnectorInfo(id="discord", label="Discord"),
        "stoat": ConnectorInfo(id="stoat", label="Stoat"),
        "irc": ConnectorInfo(id="irc", label="IRC"),
    }


# ---------------------------------------------------------------- ConnectorInfo capability flags


async def _none(_id):
    return None


def test_connector_info_capability_flags_follow_the_wired_hooks():
    irc = ConnectorInfo(id="irc", label="IRC")
    assert not irc.supports_roles
    assert not irc.supports_categories
    assert not irc.supports_emotes

    full = ConnectorInfo(
        id="discord",
        label="Discord",
        resolve_role_name=_none,
        resolve_category_name=_none,
        resolve_emoji_name=_none,
    )
    assert full.supports_roles
    assert full.supports_categories
    assert full.supports_emotes


# ---------------------------------------------------------------- ChannelLinker.link_channel


async def test_link_channel_creates_a_new_bridge_group(fake_db, connectors):
    linker = ChannelLinker(ChannelMappingRepository(fake_db), connectors)
    summary = await linker.link_channel(
        local_connector="stoat", local_channel_id="s1", local_channel_name="general",
        source="discord", source_id="d1", destination_id=None,
    )
    assert "Linked Discord channel 'd1'" in summary
    assert "Stoat channel 'general' (s1)" in summary


async def test_link_channel_unknown_source_raises(fake_db, connectors):
    linker = ChannelLinker(ChannelMappingRepository(fake_db), connectors)
    with pytest.raises(LinkError, match="isn't a known connector"):
        await linker.link_channel(
            local_connector="stoat", local_channel_id="s1", local_channel_name="general",
            source="nope", source_id="d1", destination_id=None,
        )


async def test_link_channel_to_itself_raises(fake_db, connectors):
    linker = ChannelLinker(ChannelMappingRepository(fake_db), connectors)
    with pytest.raises(LinkError, match="itself"):
        await linker.link_channel(
            local_connector="discord", local_channel_id="d1", local_channel_name="general",
            source="discord", source_id="d1", destination_id=None,
        )


async def test_link_channel_reuses_existing_group_on_either_side(fake_db, connectors):
    channel_mappings = ChannelMappingRepository(fake_db)
    linker = ChannelLinker(channel_mappings, connectors)
    await linker.link_channel(
        local_connector="stoat", local_channel_id="s1", local_channel_name="general",
        source="discord", source_id="d1", destination_id=None,
    )
    # link a third connector's channel to the *source* side of the existing pair
    await linker.link_channel(
        local_connector="irc", local_channel_id="#general", local_channel_name="#general",
        source="discord", source_id="d1", destination_id=None,
    )
    group = await channel_mappings.get_bridge_group("discord", "d1")
    mapped = await channel_mappings.get_mapped_channels(group)
    assert {m.connector_id for m in mapped} == {"discord", "stoat", "irc"}


async def test_link_channel_conflicting_groups_raises(fake_db, connectors):
    channel_mappings = ChannelMappingRepository(fake_db)
    linker = ChannelLinker(channel_mappings, connectors)
    await linker.link_channel(
        local_connector="stoat", local_channel_id="s1", local_channel_name="general",
        source="discord", source_id="d1", destination_id=None,
    )
    await linker.link_channel(
        local_connector="irc", local_channel_id="#other", local_channel_name="#other",
        source="discord", source_id="d2", destination_id=None,
    )
    # d1 is in stoat/s1's group, d2 is in irc/#other's group - linking them together should conflict
    with pytest.raises(LinkError, match="different bridge groups"):
        await linker.link_channel(
            local_connector="irc", local_channel_id="#other", local_channel_name="#other",
            source="discord", source_id="d1", destination_id=None,
        )


async def test_link_channel_notifies_on_channel_linked_hook(fake_db):
    notified = []

    async def on_linked(channel_id):
        notified.append(channel_id)

    connectors = {
        "discord": ConnectorInfo(id="discord", label="Discord"),
        "irc": ConnectorInfo(id="irc", label="IRC", on_channel_linked=on_linked),
    }
    linker = ChannelLinker(ChannelMappingRepository(fake_db), connectors)
    await linker.link_channel(
        local_connector="irc", local_channel_id="#general", local_channel_name="#general",
        source="discord", source_id="d1", destination_id=None,
    )
    assert notified == ["#general"]


async def test_link_channel_resolve_name_failure_falls_back_to_id(fake_db):
    async def failing_resolver(channel_id):
        raise RuntimeError("boom")

    connectors = {
        "discord": ConnectorInfo(id="discord", label="Discord", resolve_channel_name=failing_resolver),
        "stoat": ConnectorInfo(id="stoat", label="Stoat"),
    }
    linker = ChannelLinker(ChannelMappingRepository(fake_db), connectors)
    summary = await linker.link_channel(
        local_connector="stoat", local_channel_id="s1", local_channel_name="general",
        source="discord", source_id="d1", destination_id=None,
    )
    assert "channel 'd1' (d1)" in summary  # name resolution failed - fell back to the raw id


async def test_link_channel_resolves_bare_names_and_falls_back_to_id(fake_db):
    async def d_by_name(token):
        return {"announce": "111"}.get(token)

    async def s_by_name(token):
        return {"news": "999"}.get(token)

    connectors = {
        "discord": ConnectorInfo(id="discord", label="Discord", resolve_channel_id_by_name=d_by_name),
        "stoat": ConnectorInfo(id="stoat", label="Stoat", resolve_channel_id_by_name=s_by_name),
    }
    channel_mappings = ChannelMappingRepository(fake_db)
    linker = ChannelLinker(channel_mappings, connectors)
    await linker.link_channel(
        local_connector="stoat", local_channel_id="cur", local_channel_name="cur",
        source="discord", source_id="announce", destination_id="news",
    )
    assert await channel_mappings.get_bridge_group("discord", "111") is not None
    assert await channel_mappings.get_bridge_group("stoat", "999") is not None
    # a name the resolver doesn't know is kept as a literal id
    await linker.link_channel(
        local_connector="stoat", local_channel_id="cur2", local_channel_name="cur2",
        source="discord", source_id="raw-token", destination_id=None,
    )
    assert await channel_mappings.get_bridge_group("discord", "raw-token") is not None


# ---------------------------------------------------------------- ChannelLinker.list_linked_channels


async def test_list_linked_channels_reports_unlinked(fake_db, connectors):
    linker = ChannelLinker(ChannelMappingRepository(fake_db), connectors)
    summary = await linker.list_linked_channels(local_connector="stoat", local_channel_id="s1")
    assert summary == "This channel isn't linked to any others."


async def test_list_linked_channels_lists_every_channel_in_the_group(fake_db, connectors):
    channel_mappings = ChannelMappingRepository(fake_db)
    linker = ChannelLinker(channel_mappings, connectors)
    await linker.link_channel(
        local_connector="stoat", local_channel_id="s1", local_channel_name="general",
        source="discord", source_id="d1", destination_id=None,
    )
    await linker.link_channel(
        local_connector="irc", local_channel_id="#general", local_channel_name="#general",
        source="discord", source_id="d1", destination_id=None,
    )

    summary = await linker.list_linked_channels(local_connector="stoat", local_channel_id="s1")

    # Discord's name is "d1" (not "general") because the fixture's Discord
    # ConnectorInfo has no resolve_channel_name - link_channel only ever
    # learns the *local*/destination side's real name from its caller.
    assert "Discord: d1 (d1)" in summary
    assert "Stoat: general (s1) (this channel)" in summary
    assert "IRC: #general (#general)" in summary


async def test_list_linked_channels_marks_only_the_invoking_channel(fake_db, connectors):
    channel_mappings = ChannelMappingRepository(fake_db)
    linker = ChannelLinker(channel_mappings, connectors)
    await linker.link_channel(
        local_connector="stoat", local_channel_id="s1", local_channel_name="general",
        source="discord", source_id="d1", destination_id=None,
    )

    summary = await linker.list_linked_channels(local_connector="discord", local_channel_id="d1")

    assert "Discord: d1 (d1) (this channel)" in summary
    assert "Stoat: general (s1)" in summary
    assert "Stoat: general (s1) (this channel)" not in summary


async def test_list_linked_channels_falls_back_to_the_raw_id_for_an_unknown_connector(fake_db):
    channel_mappings = ChannelMappingRepository(fake_db)
    linker = ChannelLinker(channel_mappings, {"stoat": ConnectorInfo(id="stoat", label="Stoat")})
    await channel_mappings.upsert(
        ChannelMapping(bridge_group="g1", connector_id="stoat", channel_id="s1", channel_name="general")
    )
    await channel_mappings.upsert(
        ChannelMapping(bridge_group="g1", connector_id="webchat", channel_id="w1", channel_name="general")
    )

    summary = await linker.list_linked_channels(local_connector="stoat", local_channel_id="s1")

    assert "webchat: general (w1)" in summary


# ---------------------------------------------------------------- ChannelLinker.unlink_channel


async def test_unlink_channel_unlinked_channel_raises(fake_db, connectors):
    linker = ChannelLinker(ChannelMappingRepository(fake_db), connectors)
    with pytest.raises(LinkError, match="isn't linked"):
        await linker.unlink_channel(local_connector="stoat", local_channel_id="s1", destination=None)


async def test_unlink_channel_unknown_destination_raises(fake_db, connectors):
    channel_mappings = ChannelMappingRepository(fake_db)
    linker = ChannelLinker(channel_mappings, connectors)
    await linker.link_channel(
        local_connector="stoat", local_channel_id="s1", local_channel_name="general",
        source="discord", source_id="d1", destination_id=None,
    )

    with pytest.raises(LinkError, match="isn't linked in this channel's bridge group"):
        await linker.unlink_channel(local_connector="stoat", local_channel_id="s1", destination="irc")


async def test_unlink_channel_specific_destination_kicks_only_that_member(fake_db, connectors):
    channel_mappings = ChannelMappingRepository(fake_db)
    linker = ChannelLinker(channel_mappings, connectors)
    await linker.link_channel(
        local_connector="stoat", local_channel_id="s1", local_channel_name="general",
        source="discord", source_id="d1", destination_id=None,
    )
    await linker.link_channel(
        local_connector="irc", local_channel_id="#general", local_channel_name="#general",
        source="discord", source_id="d1", destination_id=None,
    )

    summary = await linker.unlink_channel(local_connector="stoat", local_channel_id="s1", destination="discord")

    assert "Unlinked Discord channel 'd1' (d1)" in summary
    remaining = await linker.list_linked_channels(local_connector="stoat", local_channel_id="s1")
    assert "Discord" not in remaining
    assert "IRC: #general (#general)" in remaining
    assert "Stoat: general (s1) (this channel)" in remaining


async def test_unlink_channel_all_dissolves_the_whole_group(fake_db, connectors):
    channel_mappings = ChannelMappingRepository(fake_db)
    linker = ChannelLinker(channel_mappings, connectors)
    await linker.link_channel(
        local_connector="stoat", local_channel_id="s1", local_channel_name="general",
        source="discord", source_id="d1", destination_id=None,
    )

    summary = await linker.unlink_channel(local_connector="stoat", local_channel_id="s1", destination="all")

    assert "2 channel(s) removed" in summary
    assert await channel_mappings.get_bridge_group("stoat", "s1") is None
    assert await channel_mappings.get_bridge_group("discord", "d1") is None


async def test_unlink_channel_notifies_on_channel_unlinked_hook_for_one_member(fake_db):
    parted = []

    async def on_unlinked(channel_id, unlinked_from):
        parted.append((channel_id, unlinked_from))

    connectors = {
        "discord": ConnectorInfo(id="discord", label="Discord"),
        "irc": ConnectorInfo(id="irc", label="IRC", on_channel_unlinked=on_unlinked),
    }
    linker = ChannelLinker(ChannelMappingRepository(fake_db), connectors)
    await linker.link_channel(
        local_connector="irc", local_channel_id="#general", local_channel_name="#general",
        source="discord", source_id="d1", destination_id=None,
    )

    await linker.unlink_channel(local_connector="discord", local_channel_id="d1", destination="irc")

    assert parted == [("#general", "Discord 'd1'")]


async def test_unlink_channel_all_notifies_on_channel_unlinked_hook_per_member(fake_db):
    parted = []

    async def on_unlinked(channel_id, unlinked_from):
        parted.append((channel_id, unlinked_from))

    connectors = {
        "discord": ConnectorInfo(id="discord", label="Discord"),
        "irc": ConnectorInfo(id="irc", label="IRC", on_channel_unlinked=on_unlinked),
    }
    linker = ChannelLinker(ChannelMappingRepository(fake_db), connectors)
    await linker.link_channel(
        local_connector="irc", local_channel_id="#general", local_channel_name="#general",
        source="discord", source_id="d1", destination_id=None,
    )

    await linker.unlink_channel(local_connector="discord", local_channel_id="d1", destination="all")

    assert parted == [("#general", "Discord 'd1'")]  # only IRC has the hook; Discord's absence is silently skipped


async def test_unlink_channel_kick_that_strands_a_lone_survivor_dissolves_and_announces(fake_db):
    parted = []

    async def on_unlinked(channel_id, unlinked_from):
        parted.append((channel_id, unlinked_from))

    connectors = {
        "discord": ConnectorInfo(id="discord", label="Discord"),
        "stoat": ConnectorInfo(id="stoat", label="Stoat"),
        "irc": ConnectorInfo(id="irc", label="IRC", on_channel_unlinked=on_unlinked),
    }
    channel_mappings = ChannelMappingRepository(fake_db)
    linker = ChannelLinker(channel_mappings, connectors)
    await linker.link_channel(
        local_connector="irc", local_channel_id="#general", local_channel_name="#general",
        source="discord", source_id="d1", destination_id=None,
    )
    await linker.link_channel(
        local_connector="stoat", local_channel_id="s1", local_channel_name="general",
        source="discord", source_id="d1", destination_id=None,
    )

    # kicking discord leaves irc + stoat linked (2 members) - no announce
    await linker.unlink_channel(local_connector="discord", local_channel_id="d1", destination="stoat")
    assert parted == []

    # kicking stoat now strands irc alone - dissolve the group, announce irc
    await linker.unlink_channel(local_connector="discord", local_channel_id="d1", destination="irc")
    assert parted == [("#general", "Discord 'd1'")]
    assert await channel_mappings.get_bridge_group("irc", "#general") is None
    assert await channel_mappings.get_bridge_group("discord", "d1") is None


async def test_unlink_channel_defaults_to_all(fake_db, connectors):
    channel_mappings = ChannelMappingRepository(fake_db)
    linker = ChannelLinker(channel_mappings, connectors)
    await linker.link_channel(
        local_connector="stoat", local_channel_id="s1", local_channel_name="general",
        source="discord", source_id="d1", destination_id=None,
    )

    await linker.unlink_channel(local_connector="stoat", local_channel_id="s1", destination=None)

    assert await channel_mappings.get_bridge_group("discord", "d1") is None


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


# ---------------------------------------------------------------- UserLinker.link_user


async def test_link_user_creates_a_new_group(fake_db, connectors):
    linker = UserLinker(UserMappingRepository(fake_db), connectors)
    summary = await linker.link_user(
        local_connector="irc", local_user_id="Alice", source="discord", source_user_id="111"
    )
    assert "Linked Discord user '111' to IRC user 'Alice'" in summary


async def test_link_user_to_themselves_raises(fake_db, connectors):
    linker = UserLinker(UserMappingRepository(fake_db), connectors)
    with pytest.raises(LinkError, match="themselves"):
        await linker.link_user(local_connector="discord", local_user_id="111", source="discord", source_user_id="111")


async def test_link_user_strips_a_pasted_discord_mention(fake_db, connectors):
    # Stoat/IRC's /link-user has no member-picker (unlike Discord's) - a
    # Discord id typed/pasted there often arrives as a full "<@id>" mention
    # rather than the bare snowflake.
    linker = UserLinker(UserMappingRepository(fake_db), connectors)
    summary = await linker.link_user(
        local_connector="irc", local_user_id="Alice", source="discord", source_user_id="<@216591124222050304>"
    )
    assert "Linked Discord user '216591124222050304' to IRC user 'Alice'" in summary


async def test_link_user_strips_a_pasted_discord_nickname_mention(fake_db, connectors):
    linker = UserLinker(UserMappingRepository(fake_db), connectors)
    summary = await linker.link_user(
        local_connector="irc", local_user_id="Alice", source="discord", source_user_id="<@!216591124222050304>"
    )
    assert "Linked Discord user '216591124222050304' to IRC user 'Alice'" in summary


async def test_link_user_conflicting_groups_raises(fake_db, connectors):
    linker = UserLinker(UserMappingRepository(fake_db), connectors)
    await linker.link_user(local_connector="irc", local_user_id="Alice", source="discord", source_user_id="111")
    await linker.link_user(local_connector="irc", local_user_id="Bob", source="discord", source_user_id="222")

    with pytest.raises(LinkError, match="different link groups"):
        await linker.link_user(local_connector="irc", local_user_id="Bob", source="discord", source_user_id="111")


def _name_resolving_connectors(**overrides):
    async def d_by_name(token):
        return {"alice": "111", "bob": "222"}.get(token.casefold())

    async def s_by_name(token):
        return {"shriner": "01KH"}.get(token.casefold())

    base = {
        "discord": ConnectorInfo(id="discord", label="Discord", resolve_user_id_by_name=d_by_name),
        "stoat": ConnectorInfo(id="stoat", label="Stoat", resolve_user_id_by_name=s_by_name),
        "irc": ConnectorInfo(id="irc", label="IRC"),  # no hook - a nick already IS the id
    }
    base.update(overrides)
    return base


async def test_link_user_resolves_display_names_on_both_sides(fake_db):
    linker = UserLinker(UserMappingRepository(fake_db), _name_resolving_connectors())
    summary = await linker.link_user(
        local_connector="stoat", local_user_id="Shriner", source="discord", source_user_id="Alice"
    )
    assert "Linked Discord user '111' to Stoat user '01KH'." == summary


async def test_link_user_falls_back_to_the_literal_token_when_the_name_is_unknown(fake_db):
    linker = UserLinker(UserMappingRepository(fake_db), _name_resolving_connectors())
    summary = await linker.link_user(
        local_connector="irc", local_user_id="Alice", source="discord", source_user_id="999"
    )
    assert "Linked Discord user '999' to IRC user 'Alice'." == summary


async def test_unlink_user_resolves_a_display_name(fake_db):
    connectors = _name_resolving_connectors()
    linker = UserLinker(UserMappingRepository(fake_db), connectors)
    await linker.link_user(local_connector="stoat", local_user_id="01KH", source="discord", source_user_id="111")

    summary = await linker.unlink_user(local_connector="discord", local_user_id="Alice", destination="all")
    assert "entire link group" in summary


async def test_list_linked_users_resolves_a_display_name_target(fake_db):
    connectors = _name_resolving_connectors()
    linker = UserLinker(UserMappingRepository(fake_db), connectors)
    await linker.link_user(local_connector="stoat", local_user_id="01KH", source="discord", source_user_id="111")

    summary = await linker.list_linked_users(local_connector="discord", local_user_id="Alice")
    assert "Discord" in summary and "Stoat" in summary


# ---------------------------------------------------------------- UserLinker.list_linked_users


async def test_list_linked_users_reports_unlinked(fake_db, connectors):
    linker = UserLinker(UserMappingRepository(fake_db), connectors)
    summary = await linker.list_linked_users(local_connector="discord", local_user_id="111")
    assert summary == "This user isn't linked to any others."


async def test_list_linked_users_reports_none_linked_at_all(fake_db, connectors):
    linker = UserLinker(UserMappingRepository(fake_db), connectors)
    assert await linker.list_linked_users() == "No users are linked yet."


async def test_list_linked_users_resolves_real_names_live(fake_db):
    async def discord_name(user_id):
        return {"216591124222050304": "ShrinerH"}.get(user_id)

    async def stoat_name(user_id):
        return {"01KH7TH31EBY08FTQ7YC2RC4DQ": "shriner"}.get(user_id)

    connectors = {
        "discord": ConnectorInfo(id="discord", label="Discord", resolve_user_name=discord_name),
        "stoat": ConnectorInfo(id="stoat", label="Stoat", resolve_user_name=stoat_name),
    }
    linker = UserLinker(UserMappingRepository(fake_db), connectors)
    await linker.link_user(
        local_connector="discord", local_user_id="216591124222050304",
        source="stoat", source_user_id="01KH7TH31EBY08FTQ7YC2RC4DQ",
    )

    summary = await linker.list_linked_users(local_connector="discord", local_user_id="216591124222050304")

    assert "Discord: ShrinerH (216591124222050304)" in summary
    assert "Stoat: shriner (01KH7TH31EBY08FTQ7YC2RC4DQ)" in summary


async def test_list_linked_users_falls_back_to_the_raw_id_when_unresolvable(fake_db):
    async def failing_resolver(user_id):
        raise RuntimeError("boom")

    connectors = {
        "discord": ConnectorInfo(id="discord", label="Discord", resolve_user_name=failing_resolver),
        "irc": ConnectorInfo(id="irc", label="IRC"),  # no resolver at all - IRC's id already IS the name
    }
    linker = UserLinker(UserMappingRepository(fake_db), connectors)
    await linker.link_user(local_connector="discord", local_user_id="111", source="irc", source_user_id="Alice")

    summary = await linker.list_linked_users(local_connector="discord", local_user_id="111")

    # no redundant "(id)" suffix when the resolved name IS the id (fallback
    # or, for IRC, the id always being the display name to begin with)
    assert "Discord: 111" in summary
    assert "Discord: 111 (111)" not in summary
    assert "IRC: Alice" in summary
    assert "(Alice)" not in summary


async def test_list_linked_users_with_no_target_lists_every_group(fake_db, connectors):
    linker = UserLinker(UserMappingRepository(fake_db), connectors)
    await linker.link_user(local_connector="discord", local_user_id="111", source="stoat", source_user_id="s1")
    await linker.link_user(local_connector="discord", local_user_id="222", source="irc", source_user_id="Bob")

    summary = await linker.list_linked_users()

    lines = summary.splitlines()[1:]  # drop the "Linked users:" header
    assert len(lines) == 2
    assert any("111" in line and "s1" in line for line in lines)
    assert any("222" in line and "Bob" in line for line in lines)


# ---------------------------------------------------------------- UserLinker.unlink_user


async def test_unlink_user_unlinked_user_raises(fake_db, connectors):
    linker = UserLinker(UserMappingRepository(fake_db), connectors)
    with pytest.raises(LinkError, match="isn't linked"):
        await linker.unlink_user(local_connector="irc", local_user_id="Alice", destination=None)


async def test_unlink_user_unknown_destination_raises(fake_db, connectors):
    user_mappings = UserMappingRepository(fake_db)
    linker = UserLinker(user_mappings, connectors)
    await linker.link_user(local_connector="irc", local_user_id="Alice", source="discord", source_user_id="111")

    with pytest.raises(LinkError, match="isn't linked in this user's link group"):
        await linker.unlink_user(local_connector="irc", local_user_id="Alice", destination="stoat")


async def test_unlink_user_specific_destination_kicks_only_that_member(fake_db, connectors):
    user_mappings = UserMappingRepository(fake_db)
    linker = UserLinker(user_mappings, connectors)
    await linker.link_user(local_connector="irc", local_user_id="Alice", source="discord", source_user_id="111")
    await linker.link_user(local_connector="stoat", local_user_id="s1", source="discord", source_user_id="111")

    summary = await linker.unlink_user(local_connector="irc", local_user_id="Alice", destination="discord")

    assert "Unlinked Discord user '111'" in summary
    remaining = await linker.list_linked_users(local_connector="irc", local_user_id="Alice")
    assert "Discord" not in remaining
    assert "Stoat: s1" in remaining


async def test_unlink_user_all_dissolves_the_whole_group(fake_db, connectors):
    user_mappings = UserMappingRepository(fake_db)
    linker = UserLinker(user_mappings, connectors)
    await linker.link_user(local_connector="irc", local_user_id="Alice", source="discord", source_user_id="111")

    summary = await linker.unlink_user(local_connector="irc", local_user_id="Alice", destination="all")

    assert "2 identity/identities removed" in summary
    assert await user_mappings.get_link_group("irc", "Alice") is None
    assert await user_mappings.get_link_group("discord", "111") is None


async def test_unlink_user_defaults_to_all(fake_db, connectors):
    user_mappings = UserMappingRepository(fake_db)
    linker = UserLinker(user_mappings, connectors)
    await linker.link_user(local_connector="irc", local_user_id="Alice", source="discord", source_user_id="111")

    await linker.unlink_user(local_connector="irc", local_user_id="Alice", destination=None)

    assert await user_mappings.get_link_group("discord", "111") is None


async def test_unlink_user_strips_a_pasted_discord_mention(fake_db, connectors):
    user_mappings = UserMappingRepository(fake_db)
    linker = UserLinker(user_mappings, connectors)
    await linker.link_user(local_connector="irc", local_user_id="Alice", source="discord", source_user_id="111")

    summary = await linker.unlink_user(local_connector="discord", local_user_id="<@111>", destination="all")

    assert "removed" in summary
    assert await user_mappings.get_link_group("irc", "Alice") is None


# ---------------------------------------------------------------- .connectors (Discord autocomplete)


def test_channel_linker_exposes_the_connectors_it_was_given(connectors):
    linker = ChannelLinker(channel_mappings=None, connectors=connectors)
    assert linker.connectors == connectors


def test_emote_linker_exposes_the_connectors_it_was_given(connectors):
    linker = EmoteLinker(emoji_mappings=None, connectors=connectors)
    assert linker.connectors == connectors


def test_user_linker_exposes_the_connectors_it_was_given(connectors):
    linker = UserLinker(user_mappings=None, connectors=connectors)
    assert linker.connectors == connectors


# ---------------------------------------------------------------- ChannelLinker.mirror_channel / mirror_channel_all


async def test_mirror_channel_unknown_destination_raises(fake_db, connectors):
    linker = ChannelLinker(ChannelMappingRepository(fake_db), connectors)
    with pytest.raises(LinkError, match="isn't a known connector"):
        await linker.mirror_channel(
            local_connector="discord", local_channel_id="d1", local_channel_name="general", destination="nope"
        )


async def test_mirror_channel_to_own_connector_raises(fake_db, connectors):
    linker = ChannelLinker(ChannelMappingRepository(fake_db), connectors)
    with pytest.raises(LinkError, match="own connector"):
        await linker.mirror_channel(
            local_connector="discord", local_channel_id="d1", local_channel_name="general", destination="discord"
        )


async def test_mirror_channel_without_ensure_channel_reports_unsupported(fake_db, connectors):
    # none of the fixture connectors set ensure_channel (matches Discord in the real bridge)
    linker = ChannelLinker(ChannelMappingRepository(fake_db), connectors)
    summary = await linker.mirror_channel(
        local_connector="irc", local_channel_id="#general", local_channel_name="general", destination="discord"
    )
    assert "doesn't support channel creation" in summary


async def test_mirror_channel_refuses_a_source_the_bot_cant_see(fake_db):
    async def ensure_channel(name, category=None, is_thread_category=False, category_parent_channel_id=None):
        raise AssertionError("ensure_channel must not run for a hidden source channel")

    async def cant_see(_channel_id):
        return False

    connectors = {
        "discord": ConnectorInfo(id="discord", label="Discord", can_view_channel=cant_see),
        "stoat": ConnectorInfo(id="stoat", label="Stoat", ensure_channel=ensure_channel),
    }
    linker = ChannelLinker(ChannelMappingRepository(fake_db), connectors)

    with pytest.raises(LinkError, match="can't see channel"):
        await linker.mirror_channel(
            local_connector="discord", local_channel_id="d1", local_channel_name="__hidden__", destination="stoat"
        )


async def test_mirror_channel_proceeds_when_visibility_is_unknown(fake_db):
    async def ensure_channel(name, category=None, is_thread_category=False, category_parent_channel_id=None):
        return f"stoat_{name}"

    async def cant_tell(_channel_id):
        return None  # "can't tell" must not block the mirror

    connectors = {
        "discord": ConnectorInfo(id="discord", label="Discord", can_view_channel=cant_tell),
        "stoat": ConnectorInfo(id="stoat", label="Stoat", ensure_channel=ensure_channel),
    }
    linker = ChannelLinker(ChannelMappingRepository(fake_db), connectors)

    summary = await linker.mirror_channel(
        local_connector="discord", local_channel_id="d1", local_channel_name="general", destination="stoat"
    )
    assert "Linked Discord channel 'd1'" in summary


async def test_mirror_channel_creates_and_links(fake_db):
    created = {}

    async def ensure_channel(name, category=None, is_thread_category=False, category_parent_channel_id=None):
        created.setdefault(name, f"stoat_{name}")
        return created[name]

    connectors = {
        "discord": ConnectorInfo(id="discord", label="Discord"),
        "stoat": ConnectorInfo(id="stoat", label="Stoat", ensure_channel=ensure_channel),
    }
    channel_mappings = ChannelMappingRepository(fake_db)
    linker = ChannelLinker(channel_mappings, connectors)

    summary = await linker.mirror_channel(
        local_connector="discord", local_channel_id="d1", local_channel_name="general", destination="stoat"
    )
    assert "Linked Discord channel 'd1'" in summary
    assert await channel_mappings.get_bridge_group("stoat", "stoat_general") is not None


async def test_mirror_channel_new_name_is_what_ensure_channel_and_the_link_use(fake_db):
    # issue #44: `new_name` replaces the carried-over source name for the
    # counterpart, both for ensure_channel's get-or-create and the stored link.
    seen = []

    async def ensure_channel(name, category=None, is_thread_category=False, category_parent_channel_id=None):
        seen.append(name)
        return f"stoat_{name}"

    connectors = {
        "discord": ConnectorInfo(id="discord", label="Discord"),
        "stoat": ConnectorInfo(id="stoat", label="Stoat", ensure_channel=ensure_channel),
    }
    channel_mappings = ChannelMappingRepository(fake_db)
    linker = ChannelLinker(channel_mappings, connectors)

    summary = await linker.mirror_channel(
        local_connector="discord",
        local_channel_id="d1",
        local_channel_name="general",
        destination="stoat",
        new_name="lobby",
    )

    assert seen == ["lobby"]
    assert "Stoat channel 'lobby'" in summary
    group = await channel_mappings.get_bridge_group("stoat", "stoat_lobby")
    assert group is not None
    mapped = {m.connector_id: m.channel_name for m in await channel_mappings.get_mapped_channels(group)}
    assert mapped["stoat"] == "lobby"


async def test_mirror_channel_blank_new_name_falls_back_to_the_source_name(fake_db):
    seen = []

    async def ensure_channel(name, category=None, is_thread_category=False, category_parent_channel_id=None):
        seen.append(name)
        return f"stoat_{name}"

    connectors = {
        "discord": ConnectorInfo(id="discord", label="Discord"),
        "stoat": ConnectorInfo(id="stoat", label="Stoat", ensure_channel=ensure_channel),
    }
    linker = ChannelLinker(ChannelMappingRepository(fake_db), connectors)

    await linker.mirror_channel(
        local_connector="discord",
        local_channel_id="d1",
        local_channel_name="general",
        destination="stoat",
        new_name="   ",
    )
    assert seen == ["general"]


async def test_mirror_channel_stores_the_destination_normalized_name(fake_db):
    # issue #51: mirroring a channel to IRC as `danksquad` has ensure_channel
    # hand back id `#danksquad` - the stored name must be normalized to match,
    # not left as the bare `danksquad` carried over from the source.
    async def ensure_channel(name, category=None, is_thread_category=False, category_parent_channel_id=None):
        return name if name.startswith("#") else f"#{name}"

    connectors = {
        "discord": ConnectorInfo(id="discord", label="Discord"),
        "irc": ConnectorInfo(
            id="irc",
            label="IRC",
            ensure_channel=ensure_channel,
            normalize_channel_name=lambda n: n if n.startswith("#") else f"#{n}",
        ),
    }
    channel_mappings = ChannelMappingRepository(fake_db)
    linker = ChannelLinker(channel_mappings, connectors)

    summary = await linker.mirror_channel(
        local_connector="discord", local_channel_id="d1", local_channel_name="danksquad", destination="irc"
    )

    assert "IRC channel '#danksquad' (#danksquad)" in summary
    group = await channel_mappings.get_bridge_group("irc", "#danksquad")
    assert group is not None
    mapped = {m.connector_id: m.channel_name for m in await channel_mappings.get_mapped_channels(group)}
    assert mapped["irc"] == "#danksquad"


async def test_mirror_channel_from_new_name_names_the_new_local_channel(fake_db):
    seen = []

    async def ensure_channel(name, category=None, is_thread_category=False, category_parent_channel_id=None):
        seen.append(name)
        return f"discord_{name}"

    async def resolve_channel_name(channel_id):
        return "remote-general" if channel_id == "s1" else None

    connectors = {
        "discord": ConnectorInfo(id="discord", label="Discord", ensure_channel=ensure_channel),
        "stoat": ConnectorInfo(id="stoat", label="Stoat", resolve_channel_name=resolve_channel_name),
    }
    linker = ChannelLinker(ChannelMappingRepository(fake_db), connectors)

    await linker.mirror_channel_from(
        local_connector="discord", source="stoat", source_id="s1", new_name="lobby"
    )
    assert seen == ["lobby"]


async def test_mirror_channel_from_into_irc_stores_the_normalized_name(fake_db):
    # issue #51: the `MIRROR CHANNEL FROM discord <chan>` direction on IRC lands
    # in the same link_channel path - the pulled-in name must get the `#` too.
    async def ensure_channel(name, category=None, is_thread_category=False, category_parent_channel_id=None):
        return name if name.startswith("#") else f"#{name}"

    async def discord_channel_name(channel_id):
        return "danksquad" if channel_id == "d1" else None

    connectors = {
        "discord": ConnectorInfo(id="discord", label="Discord", resolve_channel_name=discord_channel_name),
        "irc": ConnectorInfo(
            id="irc",
            label="IRC",
            ensure_channel=ensure_channel,
            normalize_channel_name=lambda n: n if n.startswith("#") else f"#{n}",
        ),
    }
    channel_mappings = ChannelMappingRepository(fake_db)
    linker = ChannelLinker(channel_mappings, connectors)

    await linker.mirror_channel_from(local_connector="irc", source="discord", source_id="d1")

    group = await channel_mappings.get_bridge_group("irc", "#danksquad")
    assert group is not None
    mapped = {m.connector_id: m.channel_name for m in await channel_mappings.get_mapped_channels(group)}
    assert mapped["irc"] == "#danksquad"


async def test_mirror_channel_skips_if_already_synced(fake_db):
    calls = []

    async def ensure_channel(name, category=None, is_thread_category=False, category_parent_channel_id=None):
        calls.append(name)
        return f"stoat_{name}"

    connectors = {
        "discord": ConnectorInfo(id="discord", label="Discord"),
        "stoat": ConnectorInfo(id="stoat", label="Stoat", ensure_channel=ensure_channel),
    }
    linker = ChannelLinker(ChannelMappingRepository(fake_db), connectors)

    await linker.mirror_channel(
        local_connector="discord", local_channel_id="d1", local_channel_name="general", destination="stoat"
    )
    summary = await linker.mirror_channel(
        local_connector="discord", local_channel_id="d1", local_channel_name="general", destination="stoat"
    )
    assert "already synced" in summary
    assert calls == ["general"]  # ensure_channel was NOT called again on the second, skipped attempt


async def test_mirror_channel_reports_link_conflict_instead_of_raising(fake_db):
    async def ensure_channel(name, category=None, is_thread_category=False, category_parent_channel_id=None):
        return "stoat_existing"  # always resolves to an already-linked-elsewhere channel

    connectors = {
        "discord": ConnectorInfo(id="discord", label="Discord"),
        "irc": ConnectorInfo(id="irc", label="IRC"),
        "stoat": ConnectorInfo(id="stoat", label="Stoat", ensure_channel=ensure_channel),
    }
    channel_mappings = ChannelMappingRepository(fake_db)
    linker = ChannelLinker(channel_mappings, connectors)

    # put the local (discord/d1) side and the channel ensure_channel will
    # resolve to (stoat/stoat_existing) into two DIFFERENT existing groups,
    # so merging them via mirror_channel is a genuine conflict
    await linker.link_channel(
        local_connector="discord", local_channel_id="d1", local_channel_name="general",
        source="irc", source_id="#group-a", destination_id=None,
    )
    await linker.link_channel(
        local_connector="stoat", local_channel_id="stoat_existing", local_channel_name="existing",
        source="irc", source_id="#group-b", destination_id=None,
    )

    summary = await linker.mirror_channel(
        local_connector="discord", local_channel_id="d1", local_channel_name="general", destination="stoat"
    )
    assert "different bridge groups" in summary  # LinkError from link_channel, caught and reported, not raised


async def test_mirror_channel_all_skips_local_connector_and_reports_each(fake_db):
    async def ensure_channel(name, category=None, is_thread_category=False, category_parent_channel_id=None):
        return f"stoat_{name}"

    connectors = {
        "discord": ConnectorInfo(id="discord", label="Discord"),
        "stoat": ConnectorInfo(id="stoat", label="Stoat", ensure_channel=ensure_channel),
        "irc": ConnectorInfo(id="irc", label="IRC"),  # no ensure_channel - reports unsupported
    }
    linker = ChannelLinker(ChannelMappingRepository(fake_db), connectors)

    summary = await linker.mirror_channel_all(
        local_connector="discord", local_channel_id="d1", local_channel_name="general"
    )
    lines = summary.splitlines()
    assert len(lines) == 2  # stoat + irc, not discord (skipped as local_connector)
    assert any("Linked" in line for line in lines)
    assert any("doesn't support channel creation" in line for line in lines)


async def test_mirror_channel_forwards_category_to_ensure_channel(fake_db):
    calls = []

    async def ensure_channel(name, category=None, is_thread_category=False, category_parent_channel_id=None):
        calls.append((name, category))
        return f"stoat_{name}"

    connectors = {
        "discord": ConnectorInfo(id="discord", label="Discord"),
        "stoat": ConnectorInfo(id="stoat", label="Stoat", ensure_channel=ensure_channel),
    }
    linker = ChannelLinker(ChannelMappingRepository(fake_db), connectors)

    await linker.mirror_channel(
        local_connector="discord",
        local_channel_id="d1",
        local_channel_name="general",
        destination="stoat",
        local_channel_category="Team Alpha",
    )
    assert calls == [("general", "Team Alpha")]


async def test_mirror_channel_reads_source_metadata_and_forwards_it_to_ensure_channel(fake_db):
    ensure_calls = []

    async def describe_channel(channel_id):
        assert channel_id == "d1"
        return ChannelMetadata(description="the source topic", nsfw=True)

    async def ensure_channel(name, category=None, is_thread_category=False, category_parent_channel_id=None, *, metadata=None):
        ensure_calls.append(metadata)
        return f"stoat_{name}"

    connectors = {
        "discord": ConnectorInfo(id="discord", label="Discord", describe_channel=describe_channel),
        "stoat": ConnectorInfo(id="stoat", label="Stoat", ensure_channel=ensure_channel),
    }
    linker = ChannelLinker(ChannelMappingRepository(fake_db), connectors)

    await linker.mirror_channel(
        local_connector="discord", local_channel_id="d1", local_channel_name="general", destination="stoat"
    )
    assert ensure_calls == [ChannelMetadata(description="the source topic", nsfw=True)]


async def test_mirror_channel_omits_the_metadata_kwarg_when_the_source_has_no_describe_hook(fake_db):
    # An ensure_channel fake that doesn't accept `metadata` must still work -
    # mirror_channel only passes the kwarg when there's metadata to pass.
    seen = []

    async def ensure_channel(name, category=None, is_thread_category=False, category_parent_channel_id=None):
        seen.append(name)
        return f"stoat_{name}"

    connectors = {
        "discord": ConnectorInfo(id="discord", label="Discord"),  # no describe_channel
        "stoat": ConnectorInfo(id="stoat", label="Stoat", ensure_channel=ensure_channel),
    }
    linker = ChannelLinker(ChannelMappingRepository(fake_db), connectors)

    await linker.mirror_channel(
        local_connector="discord", local_channel_id="d1", local_channel_name="general", destination="stoat"
    )
    assert seen == ["general"]


async def test_mirror_channel_survives_a_raising_describe_channel(fake_db):
    async def describe_channel(channel_id):
        raise RuntimeError("boom")

    async def ensure_channel(name, category=None, is_thread_category=False, category_parent_channel_id=None, *, metadata=None):
        return f"stoat_{name}"

    connectors = {
        "discord": ConnectorInfo(id="discord", label="Discord", describe_channel=describe_channel),
        "stoat": ConnectorInfo(id="stoat", label="Stoat", ensure_channel=ensure_channel),
    }
    linker = ChannelLinker(ChannelMappingRepository(fake_db), connectors)

    result = await linker.mirror_channel(
        local_connector="discord", local_channel_id="d1", local_channel_name="general", destination="stoat"
    )
    assert "Linked" in result


async def test_mirror_channel_forwards_is_thread_category_to_ensure_channel(fake_db):
    calls = []

    async def ensure_channel(name, category=None, is_thread_category=False, category_parent_channel_id=None):
        calls.append((name, category, is_thread_category))
        return f"stoat_{name}"

    connectors = {
        "discord": ConnectorInfo(id="discord", label="Discord"),
        "stoat": ConnectorInfo(id="stoat", label="Stoat", ensure_channel=ensure_channel),
    }
    linker = ChannelLinker(ChannelMappingRepository(fake_db), connectors)

    await linker.mirror_channel(
        local_connector="discord",
        local_channel_id="d1",
        local_channel_name="Test Thread",
        destination="stoat",
        local_channel_category="Announcements",
        is_thread_category=True,
    )
    assert calls == [("Test Thread", "Announcements", True)]


async def test_mirror_channel_defaults_is_thread_category_to_false(fake_db):
    calls = []

    async def ensure_channel(name, category=None, is_thread_category=False, category_parent_channel_id=None):
        calls.append(is_thread_category)
        return f"stoat_{name}"

    connectors = {
        "discord": ConnectorInfo(id="discord", label="Discord"),
        "stoat": ConnectorInfo(id="stoat", label="Stoat", ensure_channel=ensure_channel),
    }
    linker = ChannelLinker(ChannelMappingRepository(fake_db), connectors)

    await linker.mirror_channel(
        local_connector="discord", local_channel_id="d1", local_channel_name="general", destination="stoat"
    )
    assert calls == [False]


async def test_mirror_channel_names_category_after_the_destinations_linked_parent(fake_db):
    calls = []

    async def ensure_channel(name, category=None, is_thread_category=False, category_parent_channel_id=None):
        calls.append((name, category))
        return f"stoat_{name}"

    async def resolve_channel_name(channel_id):
        return {"s-parent": "Bot Config"}.get(channel_id)

    connectors = {
        "discord": ConnectorInfo(id="discord", label="Discord"),
        "stoat": ConnectorInfo(
            id="stoat", label="Stoat", ensure_channel=ensure_channel, resolve_channel_name=resolve_channel_name
        ),
    }
    channel_mappings = ChannelMappingRepository(fake_db)
    linker = ChannelLinker(channel_mappings, connectors)
    await linker.link_channel(
        local_connector="discord", local_channel_id="d-parent", local_channel_name="bot-config",
        source="stoat", source_id="s-parent", destination_id=None,
    )

    await linker.mirror_channel(
        local_connector="discord", local_channel_id="d-thread", local_channel_name="cool thread",
        destination="stoat", local_channel_category="bot-config", category_from_channel_id="d-parent",
    )

    assert calls == [("cool thread", "Bot Config")]  # Stoat's own name for the parent, not "bot-config"


async def test_mirror_channel_forwards_parent_channel_id_to_ensure_channel(fake_db):
    calls = []

    async def ensure_channel(name, category=None, is_thread_category=False, category_parent_channel_id=None):
        calls.append((name, category, category_parent_channel_id))
        return f"stoat_{name}"

    async def resolve_channel_name(channel_id):
        return {"s-parent": "Bot Config"}.get(channel_id)

    connectors = {
        "discord": ConnectorInfo(id="discord", label="Discord"),
        "stoat": ConnectorInfo(
            id="stoat", label="Stoat", ensure_channel=ensure_channel, resolve_channel_name=resolve_channel_name
        ),
    }
    channel_mappings = ChannelMappingRepository(fake_db)
    linker = ChannelLinker(channel_mappings, connectors)
    await linker.link_channel(
        local_connector="discord", local_channel_id="d-parent", local_channel_name="bot-config",
        source="stoat", source_id="s-parent", destination_id=None,
    )

    await linker.mirror_channel(
        local_connector="discord", local_channel_id="d-thread", local_channel_name="cool thread",
        destination="stoat", local_channel_category="bot-config", category_from_channel_id="d-parent",
    )

    # Stoat's own channel id for the parent reaches ensure_channel, keying the binding.
    assert calls == [("cool thread", "Bot Config", "s-parent")]


async def test_mirror_channel_category_falls_back_when_parent_isnt_linked_to_destination(fake_db):
    calls = []

    async def ensure_channel(name, category=None, is_thread_category=False, category_parent_channel_id=None):
        calls.append((name, category))
        return f"stoat_{name}"

    connectors = {
        "discord": ConnectorInfo(id="discord", label="Discord"),
        "stoat": ConnectorInfo(id="stoat", label="Stoat", ensure_channel=ensure_channel),
    }
    linker = ChannelLinker(ChannelMappingRepository(fake_db), connectors)

    await linker.mirror_channel(
        local_connector="discord", local_channel_id="d-thread", local_channel_name="cool thread",
        destination="stoat", local_channel_category="bot-config", category_from_channel_id="d-parent",
    )

    assert calls == [("cool thread", "bot-config")]  # no link -> Discord parent name


async def test_mirror_channel_all_with_no_other_connectors(fake_db):
    connectors = {"discord": ConnectorInfo(id="discord", label="Discord")}
    linker = ChannelLinker(ChannelMappingRepository(fake_db), connectors)
    summary = await linker.mirror_channel_all(
        local_connector="discord", local_channel_id="d1", local_channel_name="general"
    )
    assert summary == "no other connectors configured."


# ---------------------------------------------------------------- ChannelLinker.mirror_channel_from


def _channel_from_connectors(*, ensure_calls, source_category=None):
    async def ensure_channel(name, category=None, is_thread_category=False, category_parent_channel_id=None):
        ensure_calls.append((name, category))
        return f"stoat_{name}"

    async def resolve_channel_name(cid):
        return {"d1": "general"}.get(cid, cid)

    async def resolve_channel_category(cid):
        return source_category

    return {
        "discord": ConnectorInfo(
            id="discord",
            label="Discord",
            resolve_channel_name=resolve_channel_name,
            resolve_channel_category=resolve_channel_category,
        ),
        "stoat": ConnectorInfo(id="stoat", label="Stoat", ensure_channel=ensure_channel),
    }


async def test_mirror_channel_from_creates_the_local_channel_and_links(fake_db):
    ensure_calls: list = []
    connectors = _channel_from_connectors(ensure_calls=ensure_calls)
    channel_mappings = ChannelMappingRepository(fake_db)
    linker = ChannelLinker(channel_mappings, connectors)

    summary = await linker.mirror_channel_from(local_connector="stoat", source="discord", source_id="d1")

    assert "Linked Discord channel 'general'" in summary
    assert ensure_calls == [("general", None)]
    assert await channel_mappings.get_bridge_group("stoat", "stoat_general") is not None


async def test_mirror_channel_from_lands_in_the_linked_local_category(fake_db):
    ensure_calls: list = []
    connectors = _channel_from_connectors(ensure_calls=ensure_calls, source_category=("dcat", "Discord Team"))
    channel_mappings = ChannelMappingRepository(fake_db)
    category_mappings = CategoryMappingRepository(fake_db)
    # discord category `dcat` is already linked to stoat category "Stoat Team"
    await category_mappings.upsert(
        CategoryMapping(bridge_group="g1", connector_id="discord", category_id="dcat", category_name="Discord Team")
    )
    await category_mappings.upsert(
        CategoryMapping(bridge_group="g1", connector_id="stoat", category_id="scat", category_name="Stoat Team")
    )
    linker = ChannelLinker(channel_mappings, connectors, category_mappings)

    await linker.mirror_channel_from(local_connector="stoat", source="discord", source_id="d1")

    assert ensure_calls == [("general", "Stoat Team")]


async def test_mirror_channel_from_uses_source_category_name_when_unlinked(fake_db):
    ensure_calls: list = []
    connectors = _channel_from_connectors(ensure_calls=ensure_calls, source_category=("dcat", "Discord Team"))
    linker = ChannelLinker(ChannelMappingRepository(fake_db), connectors, CategoryMappingRepository(fake_db))

    await linker.mirror_channel_from(local_connector="stoat", source="discord", source_id="d1")

    assert ensure_calls == [("general", "Discord Team")]


async def test_mirror_channel_from_own_connector_raises(fake_db, connectors):
    linker = ChannelLinker(ChannelMappingRepository(fake_db), connectors)
    with pytest.raises(LinkError, match="from a connector to itself"):
        await linker.mirror_channel_from(local_connector="discord", source="discord", source_id="d1")


async def test_mirror_channel_from_unknown_source_raises(fake_db, connectors):
    linker = ChannelLinker(ChannelMappingRepository(fake_db), connectors)
    with pytest.raises(LinkError, match="isn't a known connector"):
        await linker.mirror_channel_from(local_connector="discord", source="nope", source_id="d1")
