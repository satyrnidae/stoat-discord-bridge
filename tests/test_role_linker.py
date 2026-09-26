import pytest

from stoat_discord_bridge.admin_commands import (
    ConnectorInfo,
    LinkedMember,
    LinkError,
    NothingLinkedError,
    RoleLinker,
)
from stoat_discord_bridge.models import RoleMetadata
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


async def test_mirror_role_matches_a_same_named_role_instead_of_creating(fake_db):
    created = []

    async def create_role(name, **kwargs):
        created.append(name)
        return f"stoat_new_{name}"

    async def s_by_name(token):
        return {"Mods": "s_existing"}.get(token)

    async def d_name(role_id):
        return {"d1": "Mods"}.get(role_id)

    async def s_name(role_id):
        return {"s_existing": "Mods"}.get(role_id)

    async def describe_role(role_id):
        return RoleMetadata(color="#ff0000")

    connectors = _connectors(
        discord=ConnectorInfo(id="discord", label="Discord", resolve_role_name=d_name, describe_role=describe_role),
        stoat=ConnectorInfo(
            id="stoat",
            label="Stoat",
            create_role=create_role,
            resolve_role_id_by_name=s_by_name,
            resolve_role_name=s_name,
        ),
    )
    linker = _linker(fake_db, connectors)
    summary = await linker.mirror_role(local_connector="discord", local_role="d1", destination="stoat")
    assert summary == "Linked Discord role 'Mods' (d1) to Stoat role 'Mods' (s_existing)."
    assert created == []


async def test_mirror_role_creates_then_links(fake_db):
    created = {}

    async def create_role(name):
        created.setdefault(name, f"stoat_{name}")
        return created[name]

    async def d_name(role_id):
        return {"d1": "Mods"}.get(role_id)

    async def s_name(role_id):
        return {v: k for k, v in created.items()}.get(role_id)

    connectors = _connectors(
        discord=ConnectorInfo(id="discord", label="Discord", resolve_role_name=d_name),
        stoat=ConnectorInfo(id="stoat", label="Stoat", create_role=create_role, resolve_role_name=s_name),
    )
    linker = _linker(fake_db, connectors)
    summary = await linker.mirror_role(local_connector="discord", local_role="d1", destination="stoat")
    assert "Linked Discord role 'Mods' (d1) to Stoat role 'Mods' (stoat_Mods)." == summary
    assert created == {"Mods": "stoat_Mods"}
    # already synced -> skipped
    again = await linker.mirror_role(local_connector="discord", local_role="d1", destination="stoat")
    assert again == "Stoat: already synced - skipped."


def _metadata_connectors(describe_role, ensure_calls):
    async def create_role(name, **kwargs):
        ensure_calls.append((name, kwargs))
        return f"stoat_{name}"

    async def d_name(role_id):
        return {"d1": "Mods"}.get(role_id)

    return _connectors(
        discord=ConnectorInfo(id="discord", label="Discord", resolve_role_name=d_name, describe_role=describe_role),
        stoat=ConnectorInfo(id="stoat", label="Stoat", create_role=create_role),
    )


async def test_mirror_role_forwards_the_source_roles_metadata(fake_db):
    # issue #179: color/hoist carry over to the created counterpart.
    meta = RoleMetadata(color="#ff0000", hoist=True)
    seen = []

    async def describe_role(role_id):
        seen.append(role_id)
        return meta

    calls = []
    linker = _linker(fake_db, _metadata_connectors(describe_role, calls))
    await linker.mirror_role(local_connector="discord", local_role="d1", destination="stoat")
    assert seen == ["d1"]
    assert calls == [("Mods", {"metadata": meta})]


async def test_mirror_role_passes_no_metadata_without_a_describe_role_hook(fake_db):
    calls = []
    linker = _linker(fake_db, _metadata_connectors(None, calls))
    await linker.mirror_role(local_connector="discord", local_role="d1", destination="stoat")
    assert calls == [("Mods", {})]


async def test_mirror_role_survives_a_raising_describe_role(fake_db):
    async def describe_role(role_id):
        raise RuntimeError("boom")

    calls = []
    linker = _linker(fake_db, _metadata_connectors(describe_role, calls))
    summary = await linker.mirror_role(local_connector="discord", local_role="d1", destination="stoat")
    assert calls == [("Mods", {})]
    assert summary.startswith("Linked")


async def test_mirror_role_skips_describe_role_when_already_synced(fake_db):
    seen = []

    async def describe_role(role_id):
        seen.append(role_id)
        return RoleMetadata(color="#ff0000")

    calls = []
    linker = _linker(fake_db, _metadata_connectors(describe_role, calls))
    await linker.mirror_role(local_connector="discord", local_role="d1", destination="stoat")
    await linker.mirror_role(local_connector="discord", local_role="d1", destination="stoat")
    assert seen == ["d1"]
    assert len(calls) == 1


async def test_mirror_role_new_name_is_what_create_role_creates(fake_db):
    # issue #44: `new_name` replaces the source role name for the counterpart.
    seen = []

    async def create_role(name):
        seen.append(name)
        return f"stoat_{name}"

    async def d_name(role_id):
        return {"d1": "Mods"}.get(role_id)

    async def s_name(role_id):
        return {"stoat_Moderators": "Moderators"}.get(role_id)

    connectors = _connectors(
        discord=ConnectorInfo(id="discord", label="Discord", resolve_role_name=d_name),
        stoat=ConnectorInfo(id="stoat", label="Stoat", create_role=create_role, resolve_role_name=s_name),
    )
    linker = _linker(fake_db, connectors)
    summary = await linker.mirror_role(
        local_connector="discord", local_role="d1", destination="stoat", new_name="Moderators"
    )
    assert seen == ["Moderators"]
    assert "Stoat role 'Moderators' (stoat_Moderators)" in summary


async def test_mirror_role_from_new_name_names_the_new_local_role(fake_db):
    seen = []

    async def create_role(name):
        seen.append(name)
        return f"stoat_{name}"

    async def d_name(role_id):
        return {"d1": "Mods"}.get(role_id)

    connectors = _connectors(
        discord=ConnectorInfo(id="discord", label="Discord", resolve_role_name=d_name),
        stoat=ConnectorInfo(id="stoat", label="Stoat", create_role=create_role),
    )
    linker = _linker(fake_db, connectors)
    await linker.mirror_role_from(
        local_connector="stoat", source="discord", source_role="d1", new_name="Moderators"
    )
    assert seen == ["Moderators"]


async def test_mirror_role_unsupported_destination(fake_db):
    linker = _linker(fake_db)
    out = await linker.mirror_role(local_connector="discord", local_role="d1", destination="stoat")
    assert "doesn't support role creation" in out


async def test_mirror_role_all_one_line_per_connector(fake_db):
    async def create_role(name):
        return f"s_{name}"

    connectors = _connectors(stoat=ConnectorInfo(id="stoat", label="Stoat", create_role=create_role))
    linker = _linker(fake_db, connectors)
    out = await linker.mirror_role_all(local_connector="discord", local_role="d1")
    lines = out.splitlines()
    assert len(lines) == 2  # stoat + irc
    assert any("Stoat" in line and "Linked" in line for line in lines)
    assert any("IRC: doesn't support role creation" in line for line in lines)


async def test_mirror_role_clips_the_name_to_the_destination_limit(fake_db):
    # issue #99: a role name that fits Discord (100) is clipped to Stoat's 32.
    seen = []

    async def create_role(name):
        seen.append(name)
        return f"stoat_{name}"

    async def d_name(role_id):
        return {"d1": "R" * 40}.get(role_id)

    connectors = _connectors(
        discord=ConnectorInfo(id="discord", label="Discord", role_name_limit=100, resolve_role_name=d_name),
        stoat=ConnectorInfo(id="stoat", label="Stoat", role_name_limit=32, create_role=create_role),
    )
    linker = _linker(fake_db, connectors)
    await linker.mirror_role(local_connector="discord", local_role="d1", destination="stoat")
    assert seen == ["R" * 32]


async def test_mirror_role_within_the_limit_is_untouched(fake_db):
    seen = []

    async def create_role(name):
        seen.append(name)
        return f"stoat_{name}"

    async def d_name(role_id):
        return {"d1": "Mods"}.get(role_id)

    connectors = _connectors(
        discord=ConnectorInfo(id="discord", label="Discord", role_name_limit=100, resolve_role_name=d_name),
        stoat=ConnectorInfo(id="stoat", label="Stoat", role_name_limit=32, create_role=create_role),
    )
    linker = _linker(fake_db, connectors)
    await linker.mirror_role(local_connector="discord", local_role="d1", destination="stoat")
    assert seen == ["Mods"]


async def test_mirror_role_new_name_override_is_also_clipped(fake_db):
    seen = []

    async def create_role(name):
        seen.append(name)
        return f"stoat_{name}"

    async def d_name(role_id):
        return {"d1": "Mods"}.get(role_id)

    connectors = _connectors(
        discord=ConnectorInfo(id="discord", label="Discord", role_name_limit=100, resolve_role_name=d_name),
        stoat=ConnectorInfo(id="stoat", label="Stoat", role_name_limit=32, create_role=create_role),
    )
    linker = _linker(fake_db, connectors)
    await linker.mirror_role(
        local_connector="discord", local_role="d1", destination="stoat", new_name="M" * 50
    )
    assert seen == ["M" * 32]


# ---- mirror_role_from


async def test_mirror_role_from_creates_the_local_role(fake_db):
    created = {}

    async def create_role(name):
        created.setdefault(name, f"stoat_{name}")
        return created[name]

    async def d_name(role_id):
        return {"d1": "Mods"}.get(role_id)

    async def s_name(role_id):
        return {v: k for k, v in created.items()}.get(role_id)

    connectors = _connectors(
        discord=ConnectorInfo(id="discord", label="Discord", resolve_role_name=d_name),
        stoat=ConnectorInfo(id="stoat", label="Stoat", create_role=create_role, resolve_role_name=s_name),
    )
    linker = _linker(fake_db, connectors)

    # run "on stoat", pulling discord's role d1 in
    summary = await linker.mirror_role_from(local_connector="stoat", source="discord", source_role="d1")

    assert "Linked Discord role 'Mods' (d1) to Stoat role 'Mods' (stoat_Mods)." == summary
    assert created == {"Mods": "stoat_Mods"}


async def test_mirror_role_from_own_connector_raises(fake_db):
    with pytest.raises(LinkError, match="from a connector to itself"):
        await _linker(fake_db).mirror_role_from(local_connector="discord", source="discord", source_role="d1")


async def test_mirror_role_from_unknown_source_raises(fake_db):
    with pytest.raises(LinkError, match="isn't a known connector"):
        await _linker(fake_db).mirror_role_from(local_connector="discord", source="nope", source_role="x")


# ---- entity-level `all` (issue #123)


async def test_mirror_role_to_all_mirrors_every_local_role(fake_db):
    created = []

    async def create_role(name):
        created.append(name)
        return f"stoat_{name}"

    async def list_roles():
        return [("d1", "Mods"), ("d2", "VIPs")]

    async def d_name(role_id):
        return {"d1": "Mods", "d2": "VIPs"}.get(role_id)

    connectors = _connectors(
        discord=ConnectorInfo(id="discord", label="Discord", list_roles=list_roles, resolve_role_name=d_name),
        stoat=ConnectorInfo(id="stoat", label="Stoat", create_role=create_role),
    )
    linker = _linker(fake_db, connectors)

    summary = await linker.mirror_role(local_connector="discord", local_role="all", destination="stoat")

    assert created == ["Mods", "VIPs"]
    assert "Linked Discord role 'Mods'" in summary
    assert "Linked Discord role 'VIPs'" in summary


async def test_mirror_role_to_all_without_list_roles_raises(fake_db):
    with pytest.raises(LinkError, match="doesn't support listing roles"):
        await _linker(fake_db).mirror_role(local_connector="irc", local_role="all", destination="stoat")


async def test_mirror_role_to_all_rejects_a_new_name(fake_db):
    async def list_roles():
        return [("d1", "Mods")]

    connectors = _connectors(discord=ConnectorInfo(id="discord", label="Discord", list_roles=list_roles))
    linker = _linker(fake_db, connectors)

    with pytest.raises(LinkError, match="all.*together with a new name"):
        await linker.mirror_role(
            local_connector="discord", local_role="ALL", destination="stoat", new_name="Renamed"
        )


async def test_mirror_role_from_all_pulls_in_every_source_role(fake_db):
    created = []

    async def create_role(name):
        created.append(name)
        return f"discord_{name}"

    async def list_roles():
        return [("s1", "Mods"), ("s2", "VIPs")]

    async def s_name(role_id):
        return {"s1": "Mods", "s2": "VIPs"}.get(role_id)

    connectors = _connectors(
        stoat=ConnectorInfo(id="stoat", label="Stoat", list_roles=list_roles, resolve_role_name=s_name),
        discord=ConnectorInfo(id="discord", label="Discord", create_role=create_role),
    )
    linker = _linker(fake_db, connectors)

    summary = await linker.mirror_role_from(local_connector="discord", source="stoat", source_role="all")

    assert created == ["Mods", "VIPs"]
    assert "Linked Stoat role 'Mods'" in summary
    assert "Linked Stoat role 'VIPs'" in summary


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


# ---- unlink_role (local_role: all, issue #181)


async def test_unlink_role_all_all_dissolves_only_the_local_connectors_groups(fake_db):
    linker = _linker(fake_db)
    repo = RoleMappingRepository(fake_db)
    await linker.link_role(local_connector="stoat", local_role="s1", source="discord", source_role="d1")
    await linker.link_role(local_connector="stoat", local_role="s2", source="discord", source_role="d2")
    await linker.link_role(local_connector="irc", local_role="i3", source="discord", source_role="d3")

    summary = await linker.unlink_role(local_connector="stoat", local_role="ALL", destination="all")

    assert summary.splitlines()[0] == "Dissolved 2 bridge group(s) on Stoat:"
    for connector_id, role_id in (("stoat", "s1"), ("discord", "d1"), ("stoat", "s2"), ("discord", "d2")):
        assert await repo.get_bridge_group(connector_id, role_id) is None
    assert await repo.get_bridge_group("discord", "d3") is not None


async def test_unlink_role_all_with_service_kicks_it_and_dissolves_a_lone_survivor(fake_db):
    linker = _linker(fake_db)
    repo = RoleMappingRepository(fake_db)
    # 3-way group: kicking IRC leaves Stoat+Discord linked
    await linker.link_role(local_connector="stoat", local_role="s1", source="discord", source_role="d1")
    await linker.link_role(local_connector="irc", local_role="i1", source="discord", source_role="d1")
    # 2-way group: kicking IRC would strand Stoat alone, so it's dissolved
    await linker.link_role(local_connector="stoat", local_role="s2", source="irc", source_role="i2")
    # no IRC member: skipped
    await linker.link_role(local_connector="stoat", local_role="s3", source="discord", source_role="d3")

    summary = await linker.unlink_role(local_connector="stoat", local_role="all", destination="irc")

    assert "'s1': unlinked IRC role 'i1'" in summary and "'s3'" not in summary
    assert await repo.get_bridge_group("irc", "i1") is None
    assert await repo.get_bridge_group("stoat", "s1") == await repo.get_bridge_group("discord", "d1")
    assert await repo.get_bridge_group("stoat", "s2") is None
    assert await repo.get_bridge_group("stoat", "s3") is not None


async def test_unlink_role_all_without_service_raises(fake_db):
    linker = _linker(fake_db)
    await linker.link_role(local_connector="stoat", local_role="s1", source="discord", source_role="d1")
    with pytest.raises(LinkError, match="explicit service"):
        await linker.unlink_role(local_connector="stoat", local_role="all", destination=None)


async def test_unlink_role_all_with_nothing_linked_raises_nothing_linked_error(fake_db):
    with pytest.raises(NothingLinkedError, match="no roles on Stoat are linked"):
        await _linker(fake_db).unlink_role(local_connector="stoat", local_role="all", destination="all")


# ---- describe_group


async def test_describe_group_returns_none_for_an_unlinked_role(fake_db):
    linker = _linker(fake_db)
    assert await linker.describe_group(local_connector="stoat", local_id="s1") is None


async def test_describe_group_returns_the_group_id_and_members(fake_db):
    linker = _linker(fake_db)
    await linker.link_role(local_connector="stoat", local_role="s1", source="discord", source_role="d1")

    result = await linker.describe_group(local_connector="stoat", local_id="s1")

    assert result is not None
    group_id, members = result
    assert group_id == await RoleMappingRepository(fake_db).get_bridge_group("stoat", "s1")
    assert members == [
        LinkedMember(connector_id="discord", label="Discord", entity_id="d1", name="d1"),
        LinkedMember(connector_id="stoat", label="Stoat", entity_id="s1", name="s1"),
    ]
