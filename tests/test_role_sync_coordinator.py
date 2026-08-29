import pytest

from stoat_discord_bridge.admin_commands import ConnectorInfo
from stoat_discord_bridge.bridge import RoleSyncCoordinator
from stoat_discord_bridge.storage.role_mappings import RoleMapping, RoleMappingRepository
from stoat_discord_bridge.storage.user_mappings import UserMapping, UserMappingRepository


async def _setup(fake_db, *, grant_raises=False):
    roles = RoleMappingRepository(fake_db)
    users = UserMappingRepository(fake_db)
    await roles.upsert(RoleMapping(bridge_group="r", connector_id="discord", role_id="d-mod", role_name="Mod"))
    await roles.upsert(RoleMapping(bridge_group="r", connector_id="stoat", role_id="s-mod", role_name="Mod"))
    await users.upsert(UserMapping(link_group="u", connector_id="discord", user_id="d-user", display_name="d-user"))
    await users.upsert(UserMapping(link_group="u", connector_id="stoat", user_id="s-user", display_name="s-user"))

    calls: list[tuple] = []

    async def grant(user_id, role_id):
        calls.append(("grant", user_id, role_id))
        if grant_raises:
            raise RuntimeError("boom")

    async def revoke(user_id, role_id):
        calls.append(("revoke", user_id, role_id))

    connectors = {
        "discord": ConnectorInfo(id="discord", label="Discord"),
        "stoat": ConnectorInfo(id="stoat", label="Stoat", grant_role=grant, revoke_role=revoke),
    }
    return RoleSyncCoordinator(roles, users, connectors), calls


async def test_added_linked_role_is_granted_to_the_linked_identity(fake_db):
    coord, calls = await _setup(fake_db)
    await coord.handle("discord", "d-user", {"d-mod"}, set())
    assert calls == [("grant", "s-user", "s-mod")]


async def test_removed_linked_role_is_revoked(fake_db):
    coord, calls = await _setup(fake_db)
    await coord.handle("discord", "d-user", set(), {"d-mod"})
    assert calls == [("revoke", "s-user", "s-mod")]


async def test_unlinked_user_is_skipped(fake_db):
    coord, calls = await _setup(fake_db)
    await coord.handle("discord", "someone-else", {"d-mod"}, set())
    assert calls == []


async def test_unlinked_role_is_skipped(fake_db):
    coord, calls = await _setup(fake_db)
    await coord.handle("discord", "d-user", {"d-not-linked"}, set())
    assert calls == []


async def test_hook_exception_is_swallowed(fake_db):
    coord, calls = await _setup(fake_db, grant_raises=True)
    await coord.handle("discord", "d-user", {"d-mod"}, set())  # must not raise
    assert calls == [("grant", "s-user", "s-mod")]


async def test_echo_of_our_own_write_is_suppressed(fake_db):
    coord, calls = await _setup(fake_db)
    await coord.handle("discord", "d-user", {"d-mod"}, set())
    assert len(calls) == 1
    # stoat now emits its own member-update for the grant we just made -
    # translating back to (discord, d-user, +d-mod). It should be dropped.
    await coord.handle("stoat", "s-user", {"s-mod"}, set())
    assert len(calls) == 1


# ---- rename / delete propagation


async def _rename_setup(fake_db):
    roles = RoleMappingRepository(fake_db)
    users = UserMappingRepository(fake_db)
    await roles.upsert(RoleMapping(bridge_group="r", connector_id="discord", role_id="d-mod", role_name="Mod"))
    await roles.upsert(RoleMapping(bridge_group="r", connector_id="stoat", role_id="s-mod", role_name="Mod"))
    renames: list[tuple] = []

    async def rename(role_id, new_name):
        renames.append((role_id, new_name))

    connectors = {
        "discord": ConnectorInfo(id="discord", label="Discord", rename_role=rename),
        "stoat": ConnectorInfo(id="stoat", label="Stoat", rename_role=rename),
    }
    return RoleSyncCoordinator(roles, users, connectors), roles, renames


async def test_rename_propagates_to_linked_copies_and_refreshes_stored_names(fake_db):
    coord, roles, renames = await _rename_setup(fake_db)
    await coord.handle_role_renamed("discord", "d-mod", "Moderators")
    assert renames == [("s-mod", "Moderators")]  # only the other connector's copy
    group = await roles.get_bridge_group("discord", "d-mod")
    names = {m.connector_id: m.role_name for m in await roles.get_mapped_roles(group)}
    assert names == {"discord": "Moderators", "stoat": "Moderators"}


async def test_rename_echo_is_a_noop(fake_db):
    coord, roles, renames = await _rename_setup(fake_db)
    await coord.handle_role_renamed("discord", "d-mod", "Moderators")
    renames.clear()
    # stoat emits its own role-update echo for the rename we just did
    await coord.handle_role_renamed("stoat", "s-mod", "Moderators")
    assert renames == []


async def test_rename_of_unlinked_role_is_ignored(fake_db):
    coord, roles, renames = await _rename_setup(fake_db)
    await coord.handle_role_renamed("discord", "not-linked", "Whatever")
    assert renames == []


async def test_delete_drops_only_that_entry_and_dissolves_a_pair(fake_db):
    roles = RoleMappingRepository(fake_db)
    await roles.upsert(RoleMapping(bridge_group="r", connector_id="discord", role_id="d-mod", role_name="Mod"))
    await roles.upsert(RoleMapping(bridge_group="r", connector_id="stoat", role_id="s-mod", role_name="Mod"))
    coord = RoleSyncCoordinator(roles, UserMappingRepository(fake_db), {})
    await coord.handle_role_deleted("discord", "d-mod")
    assert await roles.get_bridge_group("discord", "d-mod") is None
    assert await roles.get_bridge_group("stoat", "s-mod") is None  # lone survivor -> dissolved


async def test_delete_from_a_trio_keeps_the_remaining_pair(fake_db):
    roles = RoleMappingRepository(fake_db)
    for cid, rid in [("discord", "d"), ("stoat", "s"), ("other", "o")]:
        await roles.upsert(RoleMapping(bridge_group="r", connector_id=cid, role_id=rid, role_name="Mod"))
    coord = RoleSyncCoordinator(roles, UserMappingRepository(fake_db), {})
    await coord.handle_role_deleted("discord", "d")
    assert await roles.get_bridge_group("discord", "d") is None
    assert await roles.get_bridge_group("stoat", "s") == "r"
    assert await roles.get_bridge_group("other", "o") == "r"
