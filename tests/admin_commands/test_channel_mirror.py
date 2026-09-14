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


# ---------------------------------------------------------------- name clipping (issue #99)


async def test_mirror_channel_clips_the_name_to_the_destination_limit(fake_db):
    seen = []

    async def ensure_channel(name, category=None, is_thread_category=False, category_parent_channel_id=None):
        seen.append(name)
        return f"stoat_{name}"

    connectors = {
        "discord": ConnectorInfo(id="discord", label="Discord", channel_name_limit=100),
        "stoat": ConnectorInfo(id="stoat", label="Stoat", channel_name_limit=32, ensure_channel=ensure_channel),
    }
    channel_mappings = ChannelMappingRepository(fake_db)
    linker = ChannelLinker(channel_mappings, connectors)

    await linker.mirror_channel(
        local_connector="discord",
        local_channel_id="d1",
        local_channel_name="a" * 40,
        destination="stoat",
    )

    assert seen == ["a" * 32]
    group = await channel_mappings.get_bridge_group("stoat", "stoat_" + "a" * 32)
    assert group is not None
    mapped = {m.connector_id: m.channel_name for m in await channel_mappings.get_mapped_channels(group)}
    assert mapped["stoat"] == "a" * 32  # stored name matches what ensure_channel was handed


async def test_mirror_channel_within_the_limit_is_byte_identical_to_before(fake_db):
    seen = []

    async def ensure_channel(name, category=None, is_thread_category=False, category_parent_channel_id=None):
        seen.append(name)
        return f"stoat_{name}"

    connectors = {
        "discord": ConnectorInfo(id="discord", label="Discord", channel_name_limit=100),
        "stoat": ConnectorInfo(id="stoat", label="Stoat", channel_name_limit=32, ensure_channel=ensure_channel),
    }
    linker = ChannelLinker(ChannelMappingRepository(fake_db), connectors)

    await linker.mirror_channel(
        local_connector="discord", local_channel_id="d1", local_channel_name="general", destination="stoat"
    )

    assert seen == ["general"]


async def test_mirror_channel_new_name_override_is_also_clipped(fake_db):
    seen = []

    async def ensure_channel(name, category=None, is_thread_category=False, category_parent_channel_id=None):
        seen.append(name)
        return f"stoat_{name}"

    connectors = {
        "discord": ConnectorInfo(id="discord", label="Discord", channel_name_limit=100),
        "stoat": ConnectorInfo(id="stoat", label="Stoat", channel_name_limit=32, ensure_channel=ensure_channel),
    }
    linker = ChannelLinker(ChannelMappingRepository(fake_db), connectors)

    await linker.mirror_channel(
        local_connector="discord",
        local_channel_id="d1",
        local_channel_name="general",
        destination="stoat",
        new_name="z" * 50,
    )

    assert seen == ["z" * 32]


async def test_mirror_channel_into_irc_normalizes_then_clips_and_the_stored_name_agrees(fake_db):
    from stoat_discord_bridge.services.irc_service.formatting import normalize_channel_name

    seen = []

    async def ensure_channel(name, category=None, is_thread_category=False, category_parent_channel_id=None):
        # IRC's ensure_channel re-normalizes (with its own CHANLEN); the id it
        # returns is that #name.
        channel = normalize_channel_name(name, 10)
        seen.append(channel)
        return channel

    connectors = {
        "discord": ConnectorInfo(id="discord", label="Discord", channel_name_limit=100),
        "irc": ConnectorInfo(
            id="irc",
            label="IRC",
            channel_name_limit=10,
            normalize_channel_name=lambda n: normalize_channel_name(n, 10),
            ensure_channel=ensure_channel,
        ),
    }
    channel_mappings = ChannelMappingRepository(fake_db)
    linker = ChannelLinker(channel_mappings, connectors)

    await linker.mirror_channel(
        local_connector="discord",
        local_channel_id="d1",
        local_channel_name="General Chat Room",
        destination="irc",
    )

    # "#general-chat-room" normalized, then clipped to 10 chars (prefix included)
    assert seen == ["#general-c"]
    group = await channel_mappings.get_bridge_group("irc", "#general-c")
    assert group is not None
    mapped = {m.connector_id: m.channel_name for m in await channel_mappings.get_mapped_channels(group)}
    assert mapped["irc"] == "#general-c"  # stored name == id, no length disagreement (issue #51)


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


# ---------------------------------------------------------------- entity-level `all` (issue #123)


async def test_mirror_channel_to_all_mirrors_every_local_channel(fake_db):
    ensured = []

    async def ensure_channel(name, category=None, is_thread_category=False, category_parent_channel_id=None):
        ensured.append(name)
        return f"stoat_{name}"

    async def list_channels():
        return [("d1", "general"), ("d2", "random")]

    connectors = {
        "discord": ConnectorInfo(id="discord", label="Discord", list_channels=list_channels),
        "stoat": ConnectorInfo(id="stoat", label="Stoat", ensure_channel=ensure_channel),
    }
    channel_mappings = ChannelMappingRepository(fake_db)
    linker = ChannelLinker(channel_mappings, connectors)

    summary = await linker.mirror_channel(
        local_connector="discord", local_channel_id="all", local_channel_name="ignored", destination="stoat"
    )

    assert ensured == ["general", "random"]
    assert "Linked Discord channel 'd1'" in summary
    assert "Linked Discord channel 'd2'" in summary
    assert await channel_mappings.get_bridge_group("stoat", "stoat_general") is not None
    assert await channel_mappings.get_bridge_group("stoat", "stoat_random") is not None


async def test_mirror_channel_to_all_is_case_insensitive(fake_db):
    async def list_channels():
        return []

    connectors = {
        "discord": ConnectorInfo(id="discord", label="Discord", list_channels=list_channels),
        "stoat": ConnectorInfo(id="stoat", label="Stoat"),
    }
    linker = ChannelLinker(ChannelMappingRepository(fake_db), connectors)
    summary = await linker.mirror_channel(
        local_connector="discord", local_channel_id="ALL", local_channel_name="ignored", destination="stoat"
    )
    assert "nothing to mirror" in summary.lower()


async def test_mirror_channel_to_all_without_list_channels_raises(fake_db):
    connectors = {
        "discord": ConnectorInfo(id="discord", label="Discord"),  # no list_channels hook
        "stoat": ConnectorInfo(id="stoat", label="Stoat"),
    }
    linker = ChannelLinker(ChannelMappingRepository(fake_db), connectors)

    with pytest.raises(LinkError, match="doesn't support listing channels"):
        await linker.mirror_channel(
            local_connector="discord", local_channel_id="all", local_channel_name="ignored", destination="stoat"
        )


async def test_mirror_channel_to_all_rejects_a_new_name(fake_db):
    async def list_channels():
        return [("d1", "general")]

    connectors = {
        "discord": ConnectorInfo(id="discord", label="Discord", list_channels=list_channels),
        "stoat": ConnectorInfo(id="stoat", label="Stoat"),
    }
    linker = ChannelLinker(ChannelMappingRepository(fake_db), connectors)

    with pytest.raises(LinkError, match="all.*together with a new name"):
        await linker.mirror_channel(
            local_connector="discord",
            local_channel_id="all",
            local_channel_name="ignored",
            destination="stoat",
            new_name="lobby",
        )


async def test_mirror_channel_to_all_reports_a_per_channel_problem_without_aborting(fake_db):
    async def ensure_channel(name, category=None, is_thread_category=False, category_parent_channel_id=None):
        if name == "general":
            raise RuntimeError("no room")
        return f"stoat_{name}"

    async def list_channels():
        return [("d1", "general"), ("d2", "random")]

    connectors = {
        "discord": ConnectorInfo(id="discord", label="Discord", list_channels=list_channels),
        "stoat": ConnectorInfo(id="stoat", label="Stoat", ensure_channel=ensure_channel),
    }
    linker = ChannelLinker(ChannelMappingRepository(fake_db), connectors)

    summary = await linker.mirror_channel(
        local_connector="discord", local_channel_id="all", local_channel_name="ignored", destination="stoat"
    )
    lines = summary.splitlines()
    assert len(lines) == 2
    assert "failed to create/find a channel" in lines[0]
    assert "Linked Discord channel 'd2'" in lines[1]


async def test_mirror_channel_all_with_no_other_connectors(fake_db):
    connectors = {"discord": ConnectorInfo(id="discord", label="Discord")}
    linker = ChannelLinker(ChannelMappingRepository(fake_db), connectors)
    summary = await linker.mirror_channel_all(
        local_connector="discord", local_channel_id="d1", local_channel_name="general"
    )
    assert summary == "no other connectors configured."


# -------------------------------------------- ChannelLinker.mirror_channel_for_thread / mirror_channel_all_for_thread


async def test_mirror_channel_for_thread_creates_uncategorized_then_defers_placement(fake_db):
    # issue #124: the create+link call must NOT carry a category - that's
    # deferred to the returned `finish` callback, so the caller can relay
    # (and pin) the thread's starter message into an uncategorized channel
    # first.
    calls = []

    async def ensure_channel(name, category=None, is_thread_category=False, category_parent_channel_id=None):
        calls.append((name, category, is_thread_category, category_parent_channel_id))
        return f"stoat_{name}"

    connectors = {
        "discord": ConnectorInfo(id="discord", label="Discord"),
        "stoat": ConnectorInfo(id="stoat", label="Stoat", ensure_channel=ensure_channel),
    }
    channel_mappings = ChannelMappingRepository(fake_db)
    linker = ChannelLinker(channel_mappings, connectors)

    summary, finish = await linker.mirror_channel_for_thread(
        local_connector="discord",
        local_channel_id="d1",
        local_channel_name="Test Thread",
        destination="stoat",
        local_channel_category="Announcements",
        category_from_channel_id="d-parent",
    )

    assert "Linked Discord channel 'd1'" in summary
    assert calls == [("Test Thread", None, True, None)]
    assert await channel_mappings.get_bridge_group("stoat", "stoat_Test Thread") is not None

    assert finish is not None
    await finish()
    assert calls == [
        ("Test Thread", None, True, None),
        ("Test Thread", "🧵 #Announcements", True, None),
    ]


async def test_mirror_channel_for_thread_uses_the_destinations_linked_parent_name(fake_db):
    calls = []

    async def ensure_channel(name, category=None, is_thread_category=False, category_parent_channel_id=None):
        calls.append((category, category_parent_channel_id))
        return f"stoat_{name}"

    async def resolve_channel_name(channel_id):
        return {"stoat_parent": "Bot Config"}.get(channel_id)

    connectors = {
        "discord": ConnectorInfo(id="discord", label="Discord"),
        "stoat": ConnectorInfo(
            id="stoat", label="Stoat", ensure_channel=ensure_channel, resolve_channel_name=resolve_channel_name
        ),
    }
    channel_mappings = ChannelMappingRepository(fake_db)
    linker = ChannelLinker(channel_mappings, connectors)
    await linker.link_channel(
        local_connector="discord", local_channel_id="d-parent", local_channel_name="general",
        source="stoat", source_id="stoat_parent", destination_id=None,
    )

    _, finish = await linker.mirror_channel_for_thread(
        local_connector="discord",
        local_channel_id="d1",
        local_channel_name="Test Thread",
        destination="stoat",
        local_channel_category="Announcements",
        category_from_channel_id="d-parent",
    )
    await finish()

    # first call (create+link) carries no category; the deferred finish()
    # call carries the destination's own name for the linked parent channel
    # ("Bot Config", not the Discord parent's "Announcements"), prefixed with
    # the thread marker (issue #98), plus the parent's Stoat channel id.
    assert calls == [(None, None), ("🧵 #Bot Config", "stoat_parent")]


async def test_mirror_channel_for_thread_unknown_destination_raises(fake_db, connectors):
    with pytest.raises(LinkError, match="isn't a known connector"):
        await ChannelLinker(ChannelMappingRepository(fake_db), connectors).mirror_channel_for_thread(
            local_connector="discord",
            local_channel_id="d1",
            local_channel_name="Test Thread",
            destination="nope",
            local_channel_category=None,
            category_from_channel_id="d-parent",
        )


async def test_mirror_channel_for_thread_to_own_connector_raises(fake_db, connectors):
    with pytest.raises(LinkError, match="own connector"):
        await ChannelLinker(ChannelMappingRepository(fake_db), connectors).mirror_channel_for_thread(
            local_connector="discord",
            local_channel_id="d1",
            local_channel_name="Test Thread",
            destination="discord",
            local_channel_category=None,
            category_from_channel_id="d-parent",
        )


async def test_mirror_channel_for_thread_skips_if_already_synced(fake_db):
    async def ensure_channel(name, category=None, is_thread_category=False, category_parent_channel_id=None):
        return f"stoat_{name}"

    connectors = {
        "discord": ConnectorInfo(id="discord", label="Discord"),
        "stoat": ConnectorInfo(id="stoat", label="Stoat", ensure_channel=ensure_channel),
    }
    channel_mappings = ChannelMappingRepository(fake_db)
    linker = ChannelLinker(channel_mappings, connectors)
    await linker.link_channel(
        local_connector="discord", local_channel_id="d1", local_channel_name="Test Thread",
        source="stoat", source_id="stoat_existing", destination_id=None,
    )

    summary, finish = await linker.mirror_channel_for_thread(
        local_connector="discord",
        local_channel_id="d1",
        local_channel_name="Test Thread",
        destination="stoat",
        local_channel_category=None,
        category_from_channel_id="d-parent",
    )
    assert "already synced" in summary
    assert finish is None


async def test_mirror_channel_all_for_thread_skips_local_connector_and_collects_finishers(fake_db):
    async def ensure_channel(name, category=None, is_thread_category=False, category_parent_channel_id=None):
        return f"stoat_{name}"

    connectors = {
        "discord": ConnectorInfo(id="discord", label="Discord"),
        "stoat": ConnectorInfo(id="stoat", label="Stoat", ensure_channel=ensure_channel),
        "irc": ConnectorInfo(id="irc", label="IRC"),  # no ensure_channel - reports unsupported, no finisher
    }
    linker = ChannelLinker(ChannelMappingRepository(fake_db), connectors)

    summary, finishers = await linker.mirror_channel_all_for_thread(
        local_connector="discord",
        local_channel_id="d1",
        local_channel_name="Test Thread",
        local_channel_category="Announcements",
        category_from_channel_id="d-parent",
    )
    lines = summary.splitlines()
    assert len(lines) == 2  # stoat + irc, not discord (skipped as local_connector)
    assert any("Linked" in line for line in lines)
    assert any("doesn't support channel creation" in line for line in lines)
    assert len(finishers) == 1  # only stoat's mirror actually created/linked a channel

    await finishers[0]()  # must not raise


async def test_mirror_channel_all_for_thread_with_no_other_connectors(fake_db):
    connectors = {"discord": ConnectorInfo(id="discord", label="Discord")}
    linker = ChannelLinker(ChannelMappingRepository(fake_db), connectors)
    summary, finishers = await linker.mirror_channel_all_for_thread(
        local_connector="discord", local_channel_id="d1", local_channel_name="Test Thread",
        category_from_channel_id="d-parent",
    )
    assert summary == "no other connectors configured."
    assert finishers == []