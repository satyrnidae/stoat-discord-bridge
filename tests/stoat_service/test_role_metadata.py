"""`describe_role` / `create_role(metadata=...)` on the Stoat connector (issue #179)."""

from __future__ import annotations

from stoat_discord_bridge.models import RoleMetadata
from tests.fakes.fake_stoat import FakeClient, FakeServer
from tests.stoat_service.conftest import _make_sender


class _Role:
    def __init__(self, id, name, color=None, hoist=False, edit_raises=None):
        self.id = id
        self.name = name
        self.color = color
        self.hoist = hoist
        self.edits = []
        self._edit_raises = edit_raises

    async def edit(self, **kwargs):
        if self._edit_raises is not None:
            raise self._edit_raises
        self.edits.append(kwargs)


class _RoleServer(FakeServer):
    def __init__(self, *roles, edit_raises=None):
        super().__init__(id="s1")
        self.roles = {r.id: r for r in roles}
        self.created_roles = []
        self._edit_raises = edit_raises

    async def create_role(self, *, name):
        role = _Role(f"role-{name}", name, edit_raises=self._edit_raises)
        self.roles[role.id] = role
        self.created_roles.append(role)
        return role


def _sender(server):
    client = FakeClient()
    client.add_server(server)
    return _make_sender(client=client)


async def test_describe_role_reads_color_and_hoist():
    sender = _sender(_RoleServer(_Role("r1", "Mods", color="linear-gradient(red, blue)", hoist=True)))
    assert await sender.describe_role("r1") == RoleMetadata(color="linear-gradient(red, blue)", hoist=True)


async def test_describe_role_returns_none_for_an_unknown_role():
    sender = _sender(_RoleServer())
    assert await sender.describe_role("nope") is None


async def test_create_role_applies_metadata_via_a_follow_up_edit():
    server = _RoleServer()
    sender = _sender(server)
    role_id = await sender.create_role("Mods", metadata=RoleMetadata(color="#ff8800", hoist=True))
    [role] = server.created_roles
    assert role_id == role.id
    assert role.edits == [{"color": "#ff8800", "hoist": True}]


async def test_create_role_without_metadata_skips_the_edit():
    server = _RoleServer()
    sender = _sender(server)
    await sender.create_role("Mods")
    await sender.create_role("Admins", metadata=RoleMetadata())  # nothing to set
    assert [r.edits for r in server.created_roles] == [[], []]


async def test_create_role_still_returns_the_new_role_if_the_edit_fails():
    server = _RoleServer(edit_raises=RuntimeError("missing permission"))
    sender = _sender(server)
    role_id = await sender.create_role("Mods", metadata=RoleMetadata(color="#ff8800"))
    assert role_id == "role-Mods"


async def test_create_role_creates_even_if_a_same_named_role_exists():
    # Matching by name is resolve_role_id_by_name's job now (issue #183).
    existing = _Role("r1", "Mods")
    server = _RoleServer(existing)
    sender = _sender(server)
    assert await sender.create_role("mods") == "role-mods"
    assert [r.id for r in server.created_roles] == ["role-mods"]
