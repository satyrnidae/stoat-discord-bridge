import pytest

from stoat_discord_bridge.admin_commands import ConnectorInfo, LinkError, RoleLinker
from stoat_discord_bridge.storage.role_mappings import RoleMappingRepository


def _connectors(**overrides):
    base = {
        "discord": ConnectorInfo(id="discord", label="Discord"),
        "stoat": ConnectorInfo(id="stoat", label="Stoat"),
        "irc": ConnectorInfo(id="irc", label="IRC"),
    }
    base.update(overrides)
    return base


def _linker(fake_db, connectors=None):
    return RoleLinker(RoleMappingRepository(fake_db), connectors or _connectors())


# ---- link_role


async def test_link_role_creates_a_new_group(fake_db):
    linker = _linker(fake_db)
    summary = await linker.link_role(
        local_connector="stoat", local_role="s1", source="discord", source_role="d1"
    )
    assert "Linked Discord role 'd1' (d1) to Stoat role 's1' (s1)." == summary


async def test_link_role_unknown_source_raises(fake_db):
    with pytest.raises(LinkError, match="isn't a known connector"):
        await _linker(fake_db).link_role(
            local_connector="stoat", local_role="s1", source="nope", source_role="x"
        )


async def test_link_role_self_link_raises(fake_db):
    with pytest.raises(LinkError, match="itself"):
        await _linker(fake_db).link_role(
            local_connector="discord", local_role="d1", source="discord", source_role="d1"
        )


async def test_link_role_conflicting_groups_raises(fake_db):
    linker = _linker(fake_db)
    await linker.link_role(local_connector="stoat", local_role="s1", source="discord", source_role="d1")
    await linker.link_role(local_connector="stoat", local_role="s2", source="discord", source_role="d2")
    with pytest.raises(LinkError, match="different bridge groups"):
        await linker.link_role(local_connector="stoat", local_role="s2", source="discord", source_role="d1")


async def test_link_role_resolves_bare_names_and_falls_back_to_id(fake_db):
    async def d_by_name(token):
        return {"Mods": "111"}.get(token)

    async def d_name(role_id):
        return {"111": "Mods"}.get(role_id)

    async def s_by_name(token):
        return {"Moderators": "999"}.get(token)

    async def s_name(role_id):
        return {"999": "Moderators"}.get(role_id)

    connectors = _connectors(
        discord=ConnectorInfo(
            id="discord", label="Discord", resolve_role_id_by_name=d_by_name, resolve_role_name=d_name
        ),
        stoat=ConnectorInfo(
            id="stoat", label="Stoat", resolve_role_id_by_name=s_by_name, resolve_role_name=s_name
        ),
    )
    linker = _linker(fake_db, connectors)
    summary = await linker.link_role(
        local_connector="stoat", local_role="Moderators", source="discord", source_role="Mods"
    )
    assert "Discord role 'Mods' (111)" in summary
    assert "Stoat role 'Moderators' (999)" in summary
    # a name the resolver doesn't know is kept as a literal id
    repo = RoleMappingRepository(fake_db)
    await linker.link_role(local_connector="irc", local_role="raw-token", source="discord", source_role="Mods")
    assert await repo.get_bridge_group("irc", "raw-token") is not None


# ---- mirror_role


async def test_mirror_role_creates_or_matches_then_links(fake_db):
    created = {}

    async def ensure_role(name):
        created.setdefault(name, f"stoat_{name}")
        return created[name]

    async def d_name(role_id):
        return {"d1": "Mods"}.get(role_id)

    async def s_name(role_id):
        return {v: k for k, v in created.items()}.get(role_id)

    connectors = _connectors(
        discord=ConnectorInfo(id="discord", label="Discord", resolve_role_name=d_name),
        stoat=ConnectorInfo(id="stoat", label="Stoat", ensure_role=ensure_role, resolve_role_name=s_name),
    )
    linker = _linker(fake_db, connectors)
    summary = await linker.mirror_role(local_connector="discord", local_role="d1", destination="stoat")
    assert "Linked Discord role 'Mods' (d1) to Stoat role 'Mods' (stoat_Mods)." == summary
    assert created == {"Mods": "stoat_Mods"}
    # already synced -> skipped
    again = await linker.mirror_role(local_connector="discord", local_role="d1", destination="stoat")
    assert again == "Stoat: already synced - skipped."


async def test_mirror_role_unsupported_destination(fake_db):
    linker = _linker(fake_db)
    out = await linker.mirror_role(local_connector="discord", local_role="d1", destination="stoat")
    assert "doesn't support role creation" in out


async def test_mirror_role_all_one_line_per_connector(fake_db):
    async def ensure_role(name):
        return f"s_{name}"

    connectors = _connectors(stoat=ConnectorInfo(id="stoat", label="Stoat", ensure_role=ensure_role))
    linker = _linker(fake_db, connectors)
    out = await linker.mirror_role_all(local_connector="discord", local_role="d1")
    lines = out.splitlines()
    assert len(lines) == 2  # stoat + irc
    assert any("Stoat" in line and "Linked" in line for line in lines)
    assert any("IRC: doesn't support role creation" in line for line in lines)


# ---- list_linked_roles / unlink_role


async def test_list_linked_roles_unlinked_and_all(fake_db):
    linker = _linker(fake_db)
    assert await linker.list_linked_roles(local_connector="stoat", local_role="s1") == "This role isn't linked to any others."
    assert await linker.list_linked_roles(local_connector="stoat") == "No roles are linked yet."
    await linker.link_role(local_connector="stoat", local_role="s1", source="discord", source_role="d1")
    single = await linker.list_linked_roles(local_connector="stoat", local_role="s1")
    assert "Discord: d1" in single and "Stoat: s1" in single
    every = await linker.list_linked_roles(local_connector="stoat")
    assert "Linked roles:" in every


async def test_unlink_role_kick_one_then_all(fake_db):
    linker = _linker(fake_db)
    await linker.link_role(local_connector="stoat", local_role="s1", source="discord", source_role="d1")
    await linker.link_role(local_connector="irc", local_role="i1", source="discord", source_role="d1")
    out = await linker.unlink_role(local_connector="stoat", local_role="s1", destination="irc")
    assert "Unlinked IRC role 'i1' (i1)" in out
    remaining = await linker.list_linked_roles(local_connector="stoat", local_role="s1")
    assert "IRC" not in remaining
    out = await linker.unlink_role(local_connector="stoat", local_role="s1", destination="all")
    assert "entire bridge group" in out


async def test_unlink_role_kick_that_strands_a_lone_survivor_dissolves(fake_db):
    linker = _linker(fake_db)
    await linker.link_role(local_connector="stoat", local_role="s1", source="discord", source_role="d1")
    await linker.unlink_role(local_connector="stoat", local_role="s1", destination="discord")
    repo = RoleMappingRepository(fake_db)
    assert await repo.get_bridge_group("stoat", "s1") is None


async def test_unlink_role_not_linked_raises(fake_db):
    with pytest.raises(LinkError, match="isn't linked"):
        await _linker(fake_db).unlink_role(local_connector="stoat", local_role="s1", destination=None)
