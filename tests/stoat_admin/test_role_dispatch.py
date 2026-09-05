from __future__ import annotations

from tests.stoat_admin.conftest import FakeRoleLinker, _make_ctx, _make_sender


# ---------------------------------------------------------------- role commands


async def test_link_role_success():
    role_linker = FakeRoleLinker()
    sender = _make_sender(role_linker=role_linker)
    ctx = _make_ctx()

    await sender._link_role(ctx, "Mods", "discord", "111")

    assert role_linker.link_role_calls == [
        {"local_connector": "stoat", "local_role": "Mods", "source": "discord", "source_role": "111"}
    ]
    assert ctx.channel.sent[0]["content"] == "role linked ok"


async def test_link_role_without_a_configured_role_linker():
    sender = _make_sender(role_linker=None)
    ctx = _make_ctx()

    await sender._link_role(ctx, "Mods", "discord", "111")

    assert ctx.channel.sent[0]["content"] == "Role linking isn't configured."


async def test_mirror_and_linked_and_unlink_role_route():
    role_linker = FakeRoleLinker()
    sender = _make_sender(role_linker=role_linker)
    ctx = _make_ctx()

    await sender._mirror_role(ctx, "Mods")
    await sender._mirror_role(ctx, "Mods", "stoat")
    await sender._linked_roles(ctx)
    await sender._unlink_role(ctx, "Mods", "all")

    assert role_linker.mirror_role_all_calls == [{"local_connector": "stoat", "local_role": "Mods"}]
    assert role_linker.mirror_role_calls == [
        {"local_connector": "stoat", "local_role": "Mods", "destination": "stoat", "new_name": None}
    ]
    assert role_linker.list_linked_roles_calls == [
        {"local_connector": "stoat", "local_role": None, "service": None}
    ]
    assert role_linker.unlink_role_calls == [
        {"local_connector": "stoat", "local_role": "Mods", "destination": "all"}
    ]


async def test_linked_roles_needs_no_admin_permission():
    role_linker = FakeRoleLinker()
    sender = _make_sender(role_linker=role_linker)
    ctx = _make_ctx(manage_server=False)

    await sender._linked_roles(ctx, "Mods")

    assert role_linker.list_linked_roles_calls == [
        {"local_connector": "stoat", "local_role": "Mods", "service": None}
    ]


