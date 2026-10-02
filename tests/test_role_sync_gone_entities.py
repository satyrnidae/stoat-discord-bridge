"""RoleSyncCoordinator checks a linked role (and, for grant/revoke, the
linked user) still exists before syncing onto it. One deleted without
`/unlink` is dropped on the spot, not retried on every event (issue #217).
Only a definite "gone" from `entity_exists` counts - can't-tell leaves it."""

from __future__ import annotations

from stoat_discord_bridge.admin_commands import ConnectorInfo
from stoat_discord_bridge.bridge import RoleSyncCoordinator
from stoat_discord_bridge.services.role_sync import RolePermissionOverride
from stoat_discord_bridge.storage.channel_mappings import ChannelMapping, ChannelMappingRepository
from stoat_discord_bridge.storage.role_mappings import RoleMapping, RoleMappingRepository
from stoat_discord_bridge.storage.user_mappings import UserMapping, UserMappingRepository


def _exists(gone: set[tuple[str, str]], unsure: set[tuple[str, str]] = frozenset()):
    async def entity_exists(kind: str, entity_id: str):
        if (kind, entity_id) in unsure:
            return None
        return (kind, entity_id) not in gone

    return entity_exists


async def _setup(fake_db, *, gone=frozenset(), unsure=frozenset(), trio=False):
    roles = RoleMappingRepository(fake_db)
    users = UserMappingRepository(fake_db)
    channels = ChannelMappingRepository(fake_db)
    members = [("discord", "d-mod"), ("stoat", "s-mod")] + ([("other", "o-mod")] if trio else [])
    for connector_id, role_id in members:
        await roles.upsert(RoleMapping(bridge_group="r", connector_id=connector_id, role_id=role_id, role_name="Mod"))
    await users.upsert(UserMapping(link_group="u", connector_id="discord", user_id="d-user", display_name="d-user"))
    await users.upsert(UserMapping(link_group="u", connector_id="stoat", user_id="s-user", display_name="s-user"))
    await channels.upsert(ChannelMapping(bridge_group="c", connector_id="discord", channel_id="d-ch", channel_name="g"))
    await channels.upsert(ChannelMapping(bridge_group="c", connector_id="stoat", channel_id="s-ch", channel_name="g"))

    calls: list[tuple] = []

    async def grant(user_id, role_id):
        calls.append(("grant", user_id, role_id))

    async def rename(role_id, new_name):
        calls.append(("rename", role_id, new_name))

    async def set_perm(channel_id, role_id, override):
        calls.append(("perm", channel_id, role_id))

    stoat = ConnectorInfo(
        id="stoat",
        label="Stoat",
        grant_role=grant,
        rename_role=rename,
        set_channel_role_permission=set_perm,
        entity_exists=_exists(set(gone), set(unsure)),
    )
    connectors = {"discord": ConnectorInfo(id="discord", label="Discord"), "stoat": stoat}
    if trio:
        connectors["other"] = ConnectorInfo(id="other", label="Other")
    coord = RoleSyncCoordinator(roles, users, connectors, channels, None)
    return coord, roles, users, calls


async def test_grant_onto_a_deleted_role_drops_the_link(fake_db):
    coord, roles, _users, calls = await _setup(fake_db, gone={("role", "s-mod")})

    await coord.handle("discord", "d-user", {"d-mod"}, set())

    assert calls == []
    assert await roles.get_bridge_group("stoat", "s-mod") is None
    assert await roles.get_bridge_group("discord", "d-mod") is None  # lone survivor dissolved


async def test_grant_to_a_deleted_user_drops_only_that_identity(fake_db):
    coord, roles, users, calls = await _setup(fake_db, gone={("user", "s-user")})

    await coord.handle("discord", "d-user", {"d-mod"}, set())

    assert calls == []
    assert await users.get_link_group("stoat", "s-user") is None
    assert await users.get_link_group("discord", "d-user") == "u"  # user links keep a lone survivor
    assert await roles.get_bridge_group("stoat", "s-mod") == "r"


async def test_cant_tell_leaves_the_link_and_still_syncs(fake_db):
    coord, roles, _users, calls = await _setup(fake_db, unsure={("role", "s-mod"), ("user", "s-user")})

    await coord.handle("discord", "d-user", {"d-mod"}, set())

    assert calls == [("grant", "s-user", "s-mod")]
    assert await roles.get_bridge_group("stoat", "s-mod") == "r"


async def test_rename_onto_a_deleted_role_drops_it_without_resurrecting_the_group(fake_db):
    coord, roles, _users, calls = await _setup(fake_db, gone={("role", "s-mod")})

    await coord.handle_role_renamed("discord", "d-mod", "Moderators")

    assert calls == []
    assert await roles.get_mapped_roles("r") == []


async def test_rename_in_a_trio_drops_only_the_deleted_copy(fake_db):
    coord, roles, _users, calls = await _setup(fake_db, gone={("role", "s-mod")}, trio=True)

    await coord.handle_role_renamed("discord", "d-mod", "Moderators")

    assert calls == []
    names = {m.connector_id: m.role_name for m in await roles.get_mapped_roles("r")}
    assert names == {"discord": "Moderators", "other": "Moderators"}


async def test_permission_mirror_onto_a_deleted_role_drops_the_link(fake_db):
    coord, roles, _users, calls = await _setup(fake_db, gone={("role", "s-mod")})
    override = RolePermissionOverride(allow=frozenset(), deny=frozenset({"send_messages"}))

    await coord.handle_channel_role_permission("discord", "d-ch", "d-mod", override)

    assert calls == []
    assert await roles.get_bridge_group("stoat", "s-mod") is None
