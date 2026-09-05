import pytest

from stoat_discord_bridge.admin_commands import ChannelLinker, ConnectorInfo, LinkError
from stoat_discord_bridge.storage.channel_mappings import ChannelMapping, ChannelMappingRepository


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