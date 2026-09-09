"""A Discord forum channel is really a Category (its posts are threads, each
already mirrored as its own channel), so `/link channel` / `/mirror channel`
on a forum id must redirect into the Category flow rather than flat-linking
it (issue #100)."""

from __future__ import annotations

import pytest

from stoat_discord_bridge.admin_commands import CategoryLinker, ChannelLinker, ConnectorInfo, LinkError
from stoat_discord_bridge.storage.category_mappings import CategoryMappingRepository, ThreadCategoryRepository
from stoat_discord_bridge.storage.channel_mappings import ChannelMappingRepository


def _connectors(*, forum_ids=("forum1",), stoat_threads=None, discord_channel_names=None):
    created_categories: dict[str, str] = {}
    discord_channel_names = discord_channel_names or {"forum1": "ttrpg-forum"}

    async def is_forum_channel(channel_id):
        return channel_id in forum_ids

    async def discord_resolve_channel_name(channel_id):
        return discord_channel_names.get(channel_id)

    async def stoat_ensure_category(name):
        cid = created_categories.setdefault(name, f"scat-{len(created_categories)}")
        return cid

    async def stoat_resolve_category_name(category_id):
        for name, cid in created_categories.items():
            if cid == category_id:
                return name
        return None

    ensure_channel_calls: list[tuple] = []

    async def stoat_ensure_channel(name, category=None, is_thread_category=False, category_parent_channel_id=None):
        ensure_channel_calls.append((name, category))
        return f"schan-{name}"

    async def discord_channels_in_category(category_id):
        return list(stoat_threads or [])

    connectors = {
        "discord": ConnectorInfo(
            id="discord",
            label="Discord",
            is_forum_channel=is_forum_channel,
            channels_in_category=discord_channels_in_category,
            resolve_channel_name=discord_resolve_channel_name,
        ),
        "stoat": ConnectorInfo(
            id="stoat",
            label="Stoat",
            category_name_limit=32,
            ensure_category=stoat_ensure_category,
            resolve_category_name=stoat_resolve_category_name,
            ensure_channel=stoat_ensure_channel,
        ),
        "irc": ConnectorInfo(id="irc", label="IRC", ensure_channel=stoat_ensure_channel),
    }
    return connectors, created_categories, ensure_channel_calls


def _linkers(fake_db, connectors):
    channel_mappings = ChannelMappingRepository(fake_db)
    category_mappings = CategoryMappingRepository(fake_db)
    channel_linker = ChannelLinker(channel_mappings, connectors, category_mappings)
    category_linker = CategoryLinker(
        category_mappings, ThreadCategoryRepository(fake_db), channel_linker, connectors
    )
    return channel_linker, category_linker, channel_mappings, category_mappings


async def test_mirror_channel_on_a_forum_creates_a_category_not_a_flat_channel(fake_db):
    connectors, created_categories, _ = _connectors()
    channel_linker, _, channel_mappings, category_mappings = _linkers(fake_db, connectors)

    summary = await channel_linker.mirror_channel(
        local_connector="discord",
        local_channel_id="forum1",
        local_channel_name="ttrpg-forum",
        destination="stoat",
    )

    # a Category was created, titled with the forum marker...
    assert "💬 #ttrpg-forum" in created_categories
    assert "Linked Discord Category" in summary
    # ...and the forum is a Category mapping, not a flat channel mapping.
    assert await category_mappings.get_bridge_group("discord", "forum1") is not None
    assert await channel_mappings.get_bridge_group("discord", "forum1") is None


async def test_mirror_channel_on_a_forum_mirrors_its_active_threads_as_channels(fake_db):
    connectors, _, ensure_channel_calls = _connectors(stoat_threads=[("post1", "First Post"), ("post2", "Second Post")])
    channel_linker, _, channel_mappings, _ = _linkers(fake_db, connectors)

    await channel_linker.mirror_channel(
        local_connector="discord",
        local_channel_id="forum1",
        local_channel_name="ttrpg-forum",
        destination="stoat",
    )

    assert await channel_mappings.get_bridge_group("discord", "post1") is not None
    assert await channel_mappings.get_bridge_group("discord", "post2") is not None
    # each post lands in the forum's `💬 #` Category, not a `🧵 #` thread group
    assert ensure_channel_calls == [
        ("First Post", "💬 #ttrpg-forum"),
        ("Second Post", "💬 #ttrpg-forum"),
    ]


async def test_mirror_channel_on_a_normal_channel_is_unchanged(fake_db):
    connectors, created_categories, _ = _connectors()
    channel_linker, _, channel_mappings, category_mappings = _linkers(fake_db, connectors)

    summary = await channel_linker.mirror_channel(
        local_connector="discord",
        local_channel_id="normal-chan",
        local_channel_name="general",
        destination="stoat",
    )

    assert "Linked Discord channel" in summary
    assert created_categories == {}
    assert await channel_mappings.get_bridge_group("discord", "normal-chan") is not None
    assert await category_mappings.get_bridge_group("discord", "normal-chan") is None


async def test_mirror_channel_on_a_forum_toward_irc_falls_through_to_a_flat_link(fake_db):
    # IRC can't hold Categories, so the redirect doesn't apply - it keeps a
    # flat linked channel per forum post via the thread pipeline (issue #100).
    connectors, created_categories, _ = _connectors()
    channel_linker, _, channel_mappings, category_mappings = _linkers(fake_db, connectors)

    summary = await channel_linker.mirror_channel(
        local_connector="discord",
        local_channel_id="forum1",
        local_channel_name="ttrpg-forum",
        destination="irc",
    )

    assert "Linked Discord channel" in summary
    assert created_categories == {}
    assert await channel_mappings.get_bridge_group("discord", "forum1") is not None
    assert await category_mappings.get_bridge_group("discord", "forum1") is None


async def test_link_channel_on_a_forum_redirects_to_link_category(fake_db):
    connectors, _, _ = _connectors()
    channel_linker, _, channel_mappings, category_mappings = _linkers(fake_db, connectors)

    summary = await channel_linker.link_channel(
        local_connector="stoat",
        local_channel_id="invoking",
        local_channel_name="invoking",
        source="discord",
        source_id="forum1",
        destination_id="s-existing-cat",
    )

    assert "Linked Discord Category" in summary
    assert await category_mappings.get_bridge_group("discord", "forum1") is not None
    assert await category_mappings.get_bridge_group("stoat", "s-existing-cat") is not None
    assert await channel_mappings.get_bridge_group("discord", "forum1") is None


async def test_link_channel_on_a_forum_without_an_explicit_category_target_errors(fake_db):
    connectors, _, _ = _connectors()
    channel_linker, _, _, _ = _linkers(fake_db, connectors)

    with pytest.raises(LinkError, match="forum channel"):
        await channel_linker.link_channel(
            local_connector="stoat",
            local_channel_id="invoking",
            local_channel_name="invoking",
            source="discord",
            source_id="forum1",
            destination_id=None,
        )


async def test_mirror_channel_from_on_a_forum_redirects_to_the_category_flow(fake_db):
    connectors, created_categories, _ = _connectors()
    channel_linker, _, channel_mappings, category_mappings = _linkers(fake_db, connectors)

    summary = await channel_linker.mirror_channel_from(
        local_connector="stoat",
        source="discord",
        source_id="forum1",
    )

    assert "💬 #ttrpg-forum" in created_categories
    assert "Linked Discord Category" in summary
    assert await category_mappings.get_bridge_group("discord", "forum1") is not None
    assert await channel_mappings.get_bridge_group("discord", "forum1") is None
