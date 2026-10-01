"""`/link` and `/mirror` drop a link whose entity was deleted on its platform
without `/unlink` before checking for conflicts or "already synced", so
repointing it just works (issue #217). A link that's still live keeps the
existing behavior - a live conflict is still refused."""

from __future__ import annotations

import pytest

from stoat_discord_bridge.admin_commands import (
    ChannelLinker,
    ConnectorInfo,
    EmoteLinker,
    LinkError,
    RoleLinker,
    UserLinker,
)
from stoat_discord_bridge.models import CustomEmoji
from stoat_discord_bridge.storage.channel_mappings import ChannelMapping, ChannelMappingRepository
from stoat_discord_bridge.storage.emoji_mappings import EmojiMappingRepository, EmojiRef
from stoat_discord_bridge.storage.role_mappings import RoleMapping, RoleMappingRepository
from stoat_discord_bridge.storage.user_mappings import UserMapping, UserMappingRepository


def _connectors(gone: set[tuple[str, str]], unsure: frozenset = frozenset(), **stoat_hooks):
    async def entity_exists(kind: str, entity_id: str):
        if (kind, entity_id) in unsure:
            return None
        return (kind, entity_id) not in gone

    return {
        "discord": ConnectorInfo(id="discord", label="Discord"),
        "stoat": ConnectorInfo(id="stoat", label="Stoat", entity_exists=entity_exists, **stoat_hooks),
    }


# ---------------------------------------------------------------- channels


async def _channels(fake_db, *pairs: tuple[str, str, str]) -> ChannelMappingRepository:
    repo = ChannelMappingRepository(fake_db)
    for group, connector_id, channel_id in pairs:
        await repo.upsert(
            ChannelMapping(bridge_group=group, connector_id=connector_id, channel_id=channel_id, channel_name="g")
        )
    return repo


async def _channel_members(repo: ChannelMappingRepository, connector_id: str, channel_id: str) -> set:
    group = await repo.get_bridge_group(connector_id, channel_id)
    return {(m.connector_id, m.channel_id) for m in await repo.get_mapped_channels(group)} if group else set()


async def test_link_channel_drops_a_deleted_old_counterpart(fake_db):
    repo = await _channels(fake_db, ("g", "discord", "d1"), ("g", "stoat", "s-old"))
    linker = ChannelLinker(repo, _connectors({("channel", "s-old")}))

    await linker.link_channel(
        local_connector="stoat",
        local_channel_id="s-new",
        local_channel_name="general",
        source="discord",
        source_id="d1",
        destination_id=None,
    )

    assert await _channel_members(repo, "discord", "d1") == {("discord", "d1"), ("stoat", "s-new")}


async def test_link_channel_lets_a_deleted_channels_group_be_rejoined(fake_db):
    # s-old is in another group with a live discord channel - once s-old is
    # dropped, that leftover pair dissolves and no longer conflicts.
    repo = await _channels(fake_db, ("a", "discord", "d1"), ("a", "stoat", "s1"), ("b", "discord", "d2"), ("b", "stoat", "s-old"))
    linker = ChannelLinker(repo, _connectors({("channel", "s-old")}))

    await linker.link_channel(
        local_connector="stoat",
        local_channel_id="s1",
        local_channel_name="general",
        source="discord",
        source_id="d2",
        destination_id=None,
    )

    assert await _channel_members(repo, "discord", "d2") == {("discord", "d1"), ("discord", "d2"), ("stoat", "s1")}


async def test_link_channel_still_refuses_a_live_conflict(fake_db):
    repo = await _channels(fake_db, ("a", "discord", "d1"), ("a", "stoat", "s1"), ("b", "discord", "d2"), ("b", "stoat", "s2"))
    linker = ChannelLinker(repo, _connectors(set()))

    with pytest.raises(LinkError, match="different bridge groups"):
        await linker.link_channel(
            local_connector="stoat",
            local_channel_id="s1",
            local_channel_name="general",
            source="discord",
            source_id="d2",
            destination_id=None,
        )


async def test_mirror_channel_recreates_a_deleted_counterpart(fake_db):
    created: list[str] = []

    async def ensure_channel(name, category=None, is_thread_category=False, category_parent_channel_id=None):
        created.append(name)
        return "s-new", True

    repo = await _channels(fake_db, ("g", "discord", "d1"), ("g", "stoat", "s-old"))
    linker = ChannelLinker(repo, _connectors({("channel", "s-old")}, ensure_channel=ensure_channel))

    summary = await linker.mirror_channel(
        local_connector="discord", local_channel_id="d1", local_channel_name="general", destination="stoat"
    )

    assert "already synced" not in summary
    assert created == ["general"]
    assert await _channel_members(repo, "discord", "d1") == {("discord", "d1"), ("stoat", "s-new")}


async def test_mirror_channel_cant_tell_leaves_the_link(fake_db):
    repo = await _channels(fake_db, ("g", "discord", "d1"), ("g", "stoat", "s-old"))

    async def ensure_channel(*_args, **_kwargs):
        raise AssertionError("must not create a channel")

    linker = ChannelLinker(
        repo, _connectors(set(), unsure=frozenset({("channel", "s-old")}), ensure_channel=ensure_channel)
    )

    summary = await linker.mirror_channel(
        local_connector="discord", local_channel_id="d1", local_channel_name="general", destination="stoat"
    )

    assert "already synced" in summary


async def test_dropping_a_dead_channel_tells_a_dissolved_survivor(fake_db):
    unlinked: list[tuple[str, str]] = []

    async def on_channel_unlinked(channel_id, unlinked_from):
        unlinked.append((channel_id, unlinked_from))

    repo = await _channels(fake_db, ("g", "irc", "#general"), ("g", "stoat", "s-old"))
    connectors = _connectors({("channel", "s-old")})
    connectors["irc"] = ConnectorInfo(id="irc", label="IRC", on_channel_unlinked=on_channel_unlinked)
    linker = ChannelLinker(repo, connectors)

    await linker.link_channel(
        local_connector="stoat",
        local_channel_id="s-new",
        local_channel_name="general",
        source="irc",
        source_id="#general",
        destination_id=None,
    )

    assert [channel_id for channel_id, _ in unlinked] == ["#general"]
    assert await _channel_members(repo, "irc", "#general") == {("irc", "#general"), ("stoat", "s-new")}


# ---------------------------------------------------------------- roles


async def test_link_role_drops_a_deleted_old_counterpart(fake_db):
    repo = RoleMappingRepository(fake_db)
    await repo.upsert(RoleMapping(bridge_group="r", connector_id="discord", role_id="d1", role_name="Mod"))
    await repo.upsert(RoleMapping(bridge_group="r", connector_id="stoat", role_id="s-old", role_name="Mod"))
    linker = RoleLinker(repo, _connectors({("role", "s-old")}))

    await linker.link_role(local_connector="stoat", local_role="s-new", source="discord", source_role="d1")

    group = await repo.get_bridge_group("discord", "d1")
    assert {(m.connector_id, m.role_id) for m in await repo.get_mapped_roles(group)} == {
        ("discord", "d1"),
        ("stoat", "s-new"),
    }


async def test_mirror_role_recreates_a_deleted_counterpart(fake_db):
    async def create_role(name, **_kwargs):
        return "s-new"

    repo = RoleMappingRepository(fake_db)
    await repo.upsert(RoleMapping(bridge_group="r", connector_id="discord", role_id="d1", role_name="Mod"))
    await repo.upsert(RoleMapping(bridge_group="r", connector_id="stoat", role_id="s-old", role_name="Mod"))
    linker = RoleLinker(repo, _connectors({("role", "s-old")}, create_role=create_role))

    summary = await linker.mirror_role(local_connector="discord", local_role="d1", destination="stoat")

    assert "already synced" not in summary
    assert await repo.find_linked_role_id("discord", "d1", "stoat") == "s-new"


# ---------------------------------------------------------------- users


async def test_link_user_drops_a_deleted_old_identity(fake_db):
    repo = UserMappingRepository(fake_db)
    await repo.upsert(UserMapping(link_group="u", connector_id="discord", user_id="d1", display_name="d1"))
    await repo.upsert(UserMapping(link_group="u", connector_id="stoat", user_id="s-old", display_name="s-old"))
    linker = UserLinker(repo, _connectors({("user", "s-old")}))

    await linker.link_user(local_connector="stoat", local_user_id="s-new", source="discord", source_user_id="d1")

    assert {(m.connector_id, m.user_id) for m in await repo.get_mapped_users("u")} == {
        ("discord", "d1"),
        ("stoat", "s-new"),
    }


# ---------------------------------------------------------------- emotes


async def test_link_emote_drops_a_deleted_old_copy(fake_db):
    repo = EmojiMappingRepository(fake_db)
    group = await repo.try_reserve(EmojiRef(connector_id="discord", emoji_id="d1", name="blob"))
    await repo.add_refs(group, [EmojiRef(connector_id="stoat", emoji_id="s-old", name="blob")])
    linker = EmoteLinker(repo, _connectors({("emoji", "s-old")}))

    await linker.link_emote(local_connector="stoat", local_id="s-new", source="discord", source_id="d1")

    assert await repo.find_equivalent("discord", "d1", "stoat") == "s-new"
    assert await repo.get_group_id("stoat", "s-old") is None


async def test_mirror_emote_recreates_a_deleted_copy(fake_db):
    async def resolve_emoji(emoji_id):
        return CustomEmoji(native_id=emoji_id, name="blob", image_url="http://x/blob.png", animated=False)

    async def ensure_emoji(emoji):
        return CustomEmoji(native_id="s-new", name=emoji.name, image_url=emoji.image_url, animated=False)

    repo = EmojiMappingRepository(fake_db)
    group = await repo.try_reserve(EmojiRef(connector_id="discord", emoji_id="d1", name="blob"))
    await repo.add_refs(group, [EmojiRef(connector_id="stoat", emoji_id="s-old", name="blob")])
    connectors = _connectors({("emoji", "s-old")}, ensure_emoji=ensure_emoji)
    connectors["discord"] = ConnectorInfo(id="discord", label="Discord", resolve_emoji=resolve_emoji)
    linker = EmoteLinker(repo, connectors)

    summary = await linker.mirror_emote(local_connector="discord", local_emote="d1", destination="stoat")

    assert "already synced" not in summary
    assert await repo.find_equivalent("discord", "d1", "stoat") == "s-new"
