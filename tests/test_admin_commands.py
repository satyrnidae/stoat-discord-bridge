import pytest

from stoat_discord_bridge.admin_commands import (
    ChannelLinker,
    ConnectorInfo,
    EmoteLinker,
    LinkError,
    StructureMirrorer,
    UserLinker,
)
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


# ---------------------------------------------------------------- EmoteLinker.link_emote


async def test_link_emote_creates_a_new_group(fake_db, connectors):
    linker = EmoteLinker(EmojiMappingRepository(fake_db), connectors)
    summary = await linker.link_emote(local_connector="stoat", local_id="s1", source="discord", source_id="d1")
    assert "Linked Discord emote 'd1' to Stoat emote 's1'" in summary


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


async def test_link_user_conflicting_groups_raises(fake_db, connectors):
    linker = UserLinker(UserMappingRepository(fake_db), connectors)
    await linker.link_user(local_connector="irc", local_user_id="Alice", source="discord", source_user_id="111")
    await linker.link_user(local_connector="irc", local_user_id="Bob", source="discord", source_user_id="222")

    with pytest.raises(LinkError, match="different link groups"):
        await linker.link_user(local_connector="irc", local_user_id="Bob", source="discord", source_user_id="111")


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


async def test_mirror_channel_creates_and_links(fake_db):
    created = {}

    async def ensure_channel(name):
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


async def test_mirror_channel_skips_if_already_synced(fake_db):
    calls = []

    async def ensure_channel(name):
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
    async def ensure_channel(name):
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
    async def ensure_channel(name):
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


async def test_mirror_channel_all_with_no_other_connectors(fake_db):
    connectors = {"discord": ConnectorInfo(id="discord", label="Discord")}
    linker = ChannelLinker(ChannelMappingRepository(fake_db), connectors)
    summary = await linker.mirror_channel_all(
        local_connector="discord", local_channel_id="d1", local_channel_name="general"
    )
    assert summary == "no other connectors configured."


# ---------------------------------------------------------------- StructureMirrorer


def test_structure_mirrorer_unknown_source_raises():
    mirrorer = StructureMirrorer({})
    with pytest.raises(LinkError, match="structure source"):
        mirrorer.get_structure("nope")


def test_structure_mirrorer_returns_provider_result():
    sentinel = object()
    mirrorer = StructureMirrorer({"discord": lambda: sentinel})
    assert mirrorer.get_structure("discord") is sentinel
