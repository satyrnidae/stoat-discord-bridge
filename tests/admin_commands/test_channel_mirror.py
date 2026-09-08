import pytest

from stoat_discord_bridge.admin_commands import ChannelLinker, ConnectorInfo, LinkError
from stoat_discord_bridge.storage.category_mappings import CategoryMapping, CategoryMappingRepository
from stoat_discord_bridge.storage.channel_mappings import ChannelMappingRepository


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


async def test_mirror_channel_to_uses_the_linked_destination_category(fake_db):
    # The source channel's Category is linked to a *differently-named*
    # Category on the destination - the mirrored channel must land in that
    # linked Category, not a fresh same-named one (issue #50).
    calls = []

    async def ensure_channel(name, category=None, is_thread_category=False, category_parent_channel_id=None):
        calls.append((name, category))
        return f"stoat_{name}"

    async def resolve_channel_category(cid):
        return ("dcat", "Discord Team")

    connectors = {
        "discord": ConnectorInfo(id="discord", label="Discord", resolve_channel_category=resolve_channel_category),
        "stoat": ConnectorInfo(id="stoat", label="Stoat", ensure_channel=ensure_channel),
    }
    category_mappings = CategoryMappingRepository(fake_db)
    await category_mappings.upsert(
        CategoryMapping(bridge_group="g1", connector_id="discord", category_id="dcat", category_name="Discord Team")
    )
    await category_mappings.upsert(
        CategoryMapping(bridge_group="g1", connector_id="stoat", category_id="scat", category_name="Stoat Team")
    )
    linker = ChannelLinker(ChannelMappingRepository(fake_db), connectors, category_mappings)

    await linker.mirror_channel(
        local_connector="discord",
        local_channel_id="d1",
        local_channel_name="general",
        destination="stoat",
        local_channel_category="Discord Team",
    )
    assert calls == [("general", "Stoat Team")]


async def test_mirror_channel_to_falls_back_to_source_category_when_unlinked(fake_db):
    calls = []

    async def ensure_channel(name, category=None, is_thread_category=False, category_parent_channel_id=None):
        calls.append((name, category))
        return f"stoat_{name}"

    async def resolve_channel_category(cid):
        return ("dcat", "Discord Team")

    connectors = {
        "discord": ConnectorInfo(id="discord", label="Discord", resolve_channel_category=resolve_channel_category),
        "stoat": ConnectorInfo(id="stoat", label="Stoat", ensure_channel=ensure_channel),
    }
    linker = ChannelLinker(ChannelMappingRepository(fake_db), connectors, CategoryMappingRepository(fake_db))

    await linker.mirror_channel(
        local_connector="discord",
        local_channel_id="d1",
        local_channel_name="general",
        destination="stoat",
        local_channel_category="ignored",
    )
    assert calls == [("general", "Discord Team")]


async def test_mirror_channel_all_with_no_other_connectors(fake_db):
    connectors = {"discord": ConnectorInfo(id="discord", label="Discord")}
    linker = ChannelLinker(ChannelMappingRepository(fake_db), connectors)
    summary = await linker.mirror_channel_all(
        local_connector="discord", local_channel_id="d1", local_channel_name="general"
    )
    assert summary == "no other connectors configured."