import pytest

from stoat_discord_bridge.admin_commands import CategoryLinker, ChannelLinker, ConnectorInfo, LinkError
from stoat_discord_bridge.storage.category_mappings import CategoryMappingRepository, ThreadCategoryRepository
from stoat_discord_bridge.storage.channel_mappings import ChannelMappingRepository


@pytest.fixture
def connectors():
    return {
        "discord": ConnectorInfo(id="discord", label="Discord"),
        "stoat": ConnectorInfo(id="stoat", label="Stoat"),
        "irc": ConnectorInfo(id="irc", label="IRC"),
    }


def _make_linker(fake_db, connectors):
    category_mappings = CategoryMappingRepository(fake_db)
    thread_categories = ThreadCategoryRepository(fake_db)
    channel_linker = ChannelLinker(ChannelMappingRepository(fake_db), connectors)
    category_linker = CategoryLinker(category_mappings, thread_categories, channel_linker, connectors)
    return category_linker, category_mappings, thread_categories, channel_linker


# ---------------------------------------------------------------- CategoryLinker.link_category


async def test_link_category_creates_a_new_bridge_group(fake_db, connectors):
    linker, _, _, _ = _make_linker(fake_db, connectors)
    summary = await linker.link_category(
        local_connector="stoat", local_category_id="s-cat", local_category_name="Team",
        source="discord", source_id="d-cat", destination_id=None,
    )
    assert "Linked Discord Category" in summary
    assert "d-cat" in summary
    assert "s-cat" in summary
    assert "sync automatically" in summary


async def test_link_category_unknown_source_raises(fake_db, connectors):
    linker, _, _, _ = _make_linker(fake_db, connectors)
    with pytest.raises(LinkError, match="isn't a known connector"):
        await linker.link_category(
            local_connector="stoat", local_category_id="s-cat", local_category_name="Team",
            source="nope", source_id="d-cat", destination_id=None,
        )


async def test_link_category_to_itself_raises(fake_db, connectors):
    linker, _, _, _ = _make_linker(fake_db, connectors)
    with pytest.raises(LinkError, match="itself"):
        await linker.link_category(
            local_connector="discord", local_category_id="d-cat", local_category_name="Team",
            source="discord", source_id="d-cat", destination_id=None,
        )


async def test_link_category_reuses_existing_group_on_either_side(fake_db, connectors):
    linker, category_mappings, _, _ = _make_linker(fake_db, connectors)
    await linker.link_category(
        local_connector="stoat", local_category_id="s-cat", local_category_name="Team",
        source="discord", source_id="d-cat", destination_id=None,
    )
    await linker.link_category(
        local_connector="irc", local_category_id="irc-cat", local_category_name="Team",
        source="discord", source_id="d-cat", destination_id=None,
    )
    group = await category_mappings.get_bridge_group("discord", "d-cat")
    mapped = await category_mappings.get_mapped_categories(group)
    assert {m.connector_id for m in mapped} == {"discord", "stoat", "irc"}


async def test_link_category_conflicting_groups_raises(fake_db, connectors):
    linker, _, _, _ = _make_linker(fake_db, connectors)
    await linker.link_category(
        local_connector="stoat", local_category_id="s-cat", local_category_name="Team",
        source="discord", source_id="d-cat", destination_id=None,
    )
    await linker.link_category(
        local_connector="irc", local_category_id="other-cat", local_category_name="Other",
        source="discord", source_id="d-cat-2", destination_id=None,
    )
    with pytest.raises(LinkError, match="different bridge groups"):
        await linker.link_category(
            local_connector="irc", local_category_id="other-cat", local_category_name="Other",
            source="discord", source_id="d-cat", destination_id=None,
        )


async def test_link_category_rejects_a_thread_category_as_source(fake_db, connectors):
    linker, _, thread_categories, _ = _make_linker(fake_db, connectors)
    await thread_categories.bind("discord", "parent-x", "thread-cat")

    with pytest.raises(LinkError, match="thread mirroring"):
        await linker.link_category(
            local_connector="stoat", local_category_id="s-cat", local_category_name="Team",
            source="discord", source_id="thread-cat", destination_id=None,
        )


async def test_link_category_rejects_a_thread_category_as_destination(fake_db, connectors):
    linker, _, thread_categories, _ = _make_linker(fake_db, connectors)
    await thread_categories.bind("stoat", "parent-y", "s-thread-cat")

    with pytest.raises(LinkError, match="thread mirroring"):
        await linker.link_category(
            local_connector="stoat", local_category_id="s-thread-cat", local_category_name="Threads",
            source="discord", source_id="d-cat", destination_id=None,
        )


# ---------------------------------------------------------------- CategoryLinker.list_linked_categories


async def test_list_linked_categories_reports_unlinked(fake_db, connectors):
    linker, _, _, _ = _make_linker(fake_db, connectors)
    summary = await linker.list_linked_categories(local_connector="stoat", local_category_id="s-cat")
    assert "isn't linked" in summary


async def test_list_linked_categories_marks_only_the_invoking_category(fake_db, connectors):
    linker, _, _, _ = _make_linker(fake_db, connectors)
    await linker.link_category(
        local_connector="stoat", local_category_id="s-cat", local_category_name="Team",
        source="discord", source_id="d-cat", destination_id=None,
    )
    summary = await linker.list_linked_categories(local_connector="stoat", local_category_id="s-cat")
    assert "(this Category)" in summary
    # discord's row falls back to its raw id as a name, since no
    # resolve_category_name is configured for it in this fixture
    assert "Discord: d-cat (d-cat)" in summary
    assert "Stoat: Team (s-cat) (this Category)" in summary


# ---------------------------------------------------------------- CategoryLinker.unlink_category


async def test_unlink_category_unlinked_raises(fake_db, connectors):
    linker, _, _, _ = _make_linker(fake_db, connectors)
    with pytest.raises(LinkError, match="isn't linked"):
        await linker.unlink_category(local_connector="stoat", local_category_id="s-cat", destination=None)


async def test_unlink_category_specific_destination_kicks_only_that_member(fake_db, connectors):
    linker, category_mappings, _, _ = _make_linker(fake_db, connectors)
    await linker.link_category(
        local_connector="stoat", local_category_id="s-cat", local_category_name="Team",
        source="discord", source_id="d-cat", destination_id=None,
    )
    await linker.link_category(
        local_connector="irc", local_category_id="irc-cat", local_category_name="Team",
        source="discord", source_id="d-cat", destination_id=None,
    )

    summary = await linker.unlink_category(local_connector="stoat", local_category_id="s-cat", destination="discord")
    assert "Unlinked Discord Category" in summary
    assert await category_mappings.get_bridge_group("discord", "d-cat") is None
    remaining_group = await category_mappings.get_bridge_group("stoat", "s-cat")
    remaining = await category_mappings.get_mapped_categories(remaining_group)
    assert {m.connector_id for m in remaining} == {"stoat", "irc"}


async def test_unlink_category_all_dissolves_the_whole_group(fake_db, connectors):
    linker, category_mappings, _, _ = _make_linker(fake_db, connectors)
    await linker.link_category(
        local_connector="stoat", local_category_id="s-cat", local_category_name="Team",
        source="discord", source_id="d-cat", destination_id=None,
    )
    summary = await linker.unlink_category(local_connector="stoat", local_category_id="s-cat", destination="all")
    assert "2 Category(s) removed" in summary
    assert await category_mappings.get_bridge_group("stoat", "s-cat") is None
    assert await category_mappings.get_bridge_group("discord", "d-cat") is None


async def test_unlink_category_defaults_to_all(fake_db, connectors):
    linker, category_mappings, _, _ = _make_linker(fake_db, connectors)
    await linker.link_category(
        local_connector="stoat", local_category_id="s-cat", local_category_name="Team",
        source="discord", source_id="d-cat", destination_id=None,
    )
    await linker.unlink_category(local_connector="stoat", local_category_id="s-cat", destination=None)
    assert await category_mappings.get_bridge_group("discord", "d-cat") is None


# ---------------------------------------------------------------- CategoryLinker.sync_new_channel


async def test_sync_new_channel_noop_when_category_unlinked(fake_db, connectors):
    linker, _, _, channel_linker = _make_linker(fake_db, connectors)
    # no ensure_channel wired anywhere, so a real sync attempt would raise/log - proves this was skipped
    await linker.sync_new_channel(
        local_connector="stoat", local_category_id="s-cat", channel_id="s-chan", channel_name="general"
    )


async def test_sync_new_channel_mirrors_onto_every_other_linked_category_by_its_own_name(fake_db):
    calls = []

    async def ensure_channel(name, category=None, is_thread_category=False, category_parent_channel_id=None):
        calls.append((name, category))
        return f"created-{name}"

    connectors = {
        "discord": ConnectorInfo(id="discord", label="Discord", ensure_channel=ensure_channel),
        "stoat": ConnectorInfo(id="stoat", label="Stoat"),
        "irc": ConnectorInfo(id="irc", label="IRC", ensure_channel=ensure_channel),
    }
    linker, _, _, _ = _make_linker(fake_db, connectors)
    await linker.link_category(
        local_connector="stoat", local_category_id="s-cat", local_category_name="Team",
        source="discord", source_id="d-cat", destination_id=None,
    )
    await linker.link_category(
        local_connector="irc", local_category_id="irc-cat", local_category_name="Irc Team",
        source="stoat", source_id="s-cat", destination_id=None,
    )

    await linker.sync_new_channel(
        local_connector="stoat", local_category_id="s-cat", channel_id="s-chan", channel_name="announcements"
    )

    # each *other* connector's own linked Category name is used (its own row's
    # category_name), not stoat's "Team" - discord's row falls back to the raw
    # id "d-cat" since no resolve_category_name is configured for it here
    assert set(calls) == {("announcements", "d-cat"), ("announcements", "Irc Team")}


async def test_sync_new_channel_skips_the_local_connector(fake_db):
    calls = []

    async def ensure_channel(name, category=None, is_thread_category=False, category_parent_channel_id=None):
        calls.append((name, category))
        return f"created-{name}"

    connectors = {
        "discord": ConnectorInfo(id="discord", label="Discord", ensure_channel=ensure_channel),
        "stoat": ConnectorInfo(id="stoat", label="Stoat", ensure_channel=ensure_channel),
    }
    linker, _, _, _ = _make_linker(fake_db, connectors)
    await linker.link_category(
        local_connector="stoat", local_category_id="s-cat", local_category_name="Team",
        source="discord", source_id="d-cat", destination_id=None,
    )

    await linker.sync_new_channel(
        local_connector="stoat", local_category_id="s-cat", channel_id="s-chan", channel_name="announcements"
    )

    # only discord's ensure_channel should have fired - stoat is local_connector, skipped.
    # discord's own linked-Category name falls back to its raw id "d-cat"
    # since no resolve_category_name is configured for it in this fixture.
    assert calls == [("announcements", "d-cat")]


async def test_sync_new_channel_uses_destination_own_category_name_not_source_name(fake_db):
    calls = []

    async def ensure_channel(name, category=None, is_thread_category=False, category_parent_channel_id=None):
        calls.append((name, category))
        return f"created-{name}"

    connectors = {
        "discord": ConnectorInfo(id="discord", label="Discord", ensure_channel=ensure_channel),
        "stoat": ConnectorInfo(id="stoat", label="Stoat"),
    }
    linker, _, _, _ = _make_linker(fake_db, connectors)
    # stoat's Category is named "Team", discord's own linked Category is named "Alpha Squad"
    await linker.link_category(
        local_connector="discord", local_category_id="d-cat", local_category_name="Alpha Squad",
        source="stoat", source_id="s-cat", destination_id=None,
    )

    await linker.sync_new_channel(
        local_connector="stoat", local_category_id="s-cat", channel_id="s-chan", channel_name="announcements"
    )

    assert calls == [("announcements", "Alpha Squad")]


# ---------------------------------------------------------------- CategoryLinker thread-category binding


async def test_bind_thread_category_delegates_to_repository(fake_db, connectors):
    linker, _, thread_categories, _ = _make_linker(fake_db, connectors)
    await linker.bind_thread_category("discord", "parent-1", "thread-cat")
    assert await thread_categories.is_thread_category("discord", "thread-cat") is True
    assert await thread_categories.get_category_id("discord", "parent-1") == "thread-cat"


async def test_thread_category_id_and_parent_lookups_delegate(fake_db, connectors):
    linker, _, _, _ = _make_linker(fake_db, connectors)
    await linker.bind_thread_category("stoat", "parent-1", "cat-1")
    assert await linker.thread_category_id("stoat", "parent-1") == "cat-1"
    assert await linker.thread_category_parent("stoat", "cat-1") == "parent-1"
    assert await linker.thread_category_id("stoat", "missing") is None


async def test_forget_thread_category_delegates(fake_db, connectors):
    linker, _, _, _ = _make_linker(fake_db, connectors)
    await linker.bind_thread_category("stoat", "parent-1", "cat-1")
    await linker.forget_thread_category("stoat", "parent-1")
    assert await linker.thread_category_id("stoat", "parent-1") is None


# ---------------------------------------------------------------- name resolution


async def test_link_category_resolves_bare_names_and_falls_back_to_id(fake_db):
    async def d_by_name(token):
        return {"Team Chat": "d-cat"}.get(token)

    async def s_by_name(token):
        return {"Team": "s-cat"}.get(token)

    connectors = {
        "discord": ConnectorInfo(id="discord", label="Discord", resolve_category_id_by_name=d_by_name),
        "stoat": ConnectorInfo(id="stoat", label="Stoat", resolve_category_id_by_name=s_by_name),
    }
    linker, category_mappings, _, _ = _make_linker(fake_db, connectors)

    await linker.link_category(
        local_connector="stoat", local_category_id="ignored", local_category_name="",
        source="discord", source_id="Team Chat", destination_id="Team",
    )

    group = await category_mappings.get_bridge_group("discord", "d-cat")
    mapped = {m.connector_id: m.category_id for m in await category_mappings.get_mapped_categories(group)}
    assert mapped == {"discord": "d-cat", "stoat": "s-cat"}


# ---------------------------------------------------------------- CategoryLinker.mirror_category


def _ensure_category_fake():
    created = []

    async def ensure_category(name):
        created.append(name)
        return f"dest-{name}"

    return ensure_category, created


async def test_mirror_category_creates_links_and_mirrors_child_channels(fake_db):
    ensure_category, created = _ensure_category_fake()
    ensure_channel_calls = []
    moved = []

    async def ensure_channel(name, category=None, is_thread_category=False, category_parent_channel_id=None):
        ensure_channel_calls.append((name, category))
        return f"dest-chan-{name}"

    async def channels_in_category(cid):
        assert cid == "s-cat"
        return [("s-chan-1", "general"), ("s-chan-2", "linked-one")]

    async def move_channel_to_category(channel_id, category_id):
        moved.append((channel_id, category_id))

    connectors = {
        "stoat": ConnectorInfo(id="stoat", label="Stoat", channels_in_category=channels_in_category),
        "discord": ConnectorInfo(
            id="discord",
            label="Discord",
            ensure_category=ensure_category,
            ensure_channel=ensure_channel,
            move_channel_to_category=move_channel_to_category,
        ),
    }
    linker, _, _, channel_linker = _make_linker(fake_db, connectors)
    # s-chan-2 is already linked to a discord channel -> should be moved, not re-created
    await channel_linker.link_channel(
        local_connector="stoat", local_channel_id="s-chan-2", local_channel_name="linked-one",
        source="discord", source_id="d-chan-2", destination_id=None,
    )

    summary = await linker.mirror_category(
        local_connector="stoat", local_category_id="s-cat", local_category_name="Team", destination="discord"
    )

    assert created == ["Team"]
    assert moved == [("d-chan-2", "dest-Team")]
    assert ("general", "dest-Team") in ensure_channel_calls
    assert "Linked" in summary


async def test_mirror_category_new_name_titles_the_counterpart_only(fake_db):
    # issue #44: `new_name` titles the destination Category; child channels
    # still carry their own names over.
    ensure_category, created = _ensure_category_fake()
    ensure_channel_calls = []

    async def ensure_channel(name, category=None, is_thread_category=False, category_parent_channel_id=None):
        ensure_channel_calls.append((name, category))
        return f"dest-chan-{name}"

    async def channels_in_category(cid):
        return [("s-chan-1", "general")]

    connectors = {
        "stoat": ConnectorInfo(id="stoat", label="Stoat", channels_in_category=channels_in_category),
        "discord": ConnectorInfo(
            id="discord", label="Discord", ensure_category=ensure_category, ensure_channel=ensure_channel
        ),
    }
    linker, _, _, _ = _make_linker(fake_db, connectors)

    await linker.mirror_category(
        local_connector="stoat",
        local_category_id="s-cat",
        local_category_name="Team",
        destination="discord",
        new_name="Team Chat",
    )

    assert created == ["Team Chat"]
    assert ensure_channel_calls == [("general", "dest-Team Chat")]


async def test_mirror_category_from_new_name_titles_the_new_local_category(fake_db):
    ensure_category, created = _ensure_category_fake()

    async def channels_in_category(cid):
        return []

    async def resolve_category_name(cid):
        return "Remote Team" if cid == "s-cat" else None

    connectors = {
        "stoat": ConnectorInfo(
            id="stoat",
            label="Stoat",
            channels_in_category=channels_in_category,
            resolve_category_name=resolve_category_name,
        ),
        "discord": ConnectorInfo(id="discord", label="Discord", ensure_category=ensure_category),
    }
    linker, _, _, _ = _make_linker(fake_db, connectors)

    await linker.mirror_category_from(
        local_connector="discord", source="stoat", source_id="s-cat", new_name="Team Chat"
    )
    assert created == ["Team Chat"]


async def test_mirror_category_reuses_an_existing_linked_category(fake_db):
    ensure_category, created = _ensure_category_fake()

    async def channels_in_category(cid):
        return []

    connectors = {
        "stoat": ConnectorInfo(id="stoat", label="Stoat", channels_in_category=channels_in_category),
        "discord": ConnectorInfo(id="discord", label="Discord", ensure_category=ensure_category),
    }
    linker, _, _, _ = _make_linker(fake_db, connectors)
    await linker.link_category(
        local_connector="stoat", local_category_id="s-cat", local_category_name="Team",
        source="discord", source_id="d-cat", destination_id=None,
    )

    summary = await linker.mirror_category(
        local_connector="stoat", local_category_id="s-cat", local_category_name="Team", destination="discord"
    )

    assert created == []  # existing d-cat reused, no new Category created
    assert "reusing" in summary


async def test_mirror_category_reports_a_destination_that_cant_create_categories(fake_db):
    connectors = {
        "stoat": ConnectorInfo(id="stoat", label="Stoat"),
        "discord": ConnectorInfo(id="discord", label="Discord"),
    }
    linker, _, _, _ = _make_linker(fake_db, connectors)

    summary = await linker.mirror_category(
        local_connector="stoat", local_category_id="s-cat", local_category_name="Team", destination="discord"
    )

    assert "doesn't support Category creation" in summary


# ---------------------------------------------------------------- CategoryLinker.mirror_category_from


async def test_mirror_category_from_creates_the_local_category_and_links(fake_db):
    ensure_category, created = _ensure_category_fake()

    async def d_cat_name(cid):
        return {"d-cat": "Team"}.get(cid)

    async def channels_in_category(cid):
        assert cid == "d-cat"
        return []

    connectors = {
        "discord": ConnectorInfo(
            id="discord", label="Discord", resolve_category_name=d_cat_name, channels_in_category=channels_in_category
        ),
        "stoat": ConnectorInfo(id="stoat", label="Stoat", ensure_category=ensure_category),
    }
    linker, category_mappings, _, _ = _make_linker(fake_db, connectors)

    # run "on stoat", pulling discord's d-cat in
    summary = await linker.mirror_category_from(local_connector="stoat", source="discord", source_id="d-cat")

    assert created == ["Team"]
    assert "Linked" in summary
    assert await category_mappings.get_bridge_group("stoat", "dest-Team") is not None


async def test_mirror_category_from_own_connector_raises(fake_db, connectors):
    linker, _, _, _ = _make_linker(fake_db, connectors)
    with pytest.raises(LinkError, match="from a connector to itself"):
        await linker.mirror_category_from(local_connector="discord", source="discord", source_id="d-cat")
