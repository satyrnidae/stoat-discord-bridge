from __future__ import annotations

from types import SimpleNamespace

from tests.fakes.fake_stoat import FakeChannel
from tests.stoat_admin.conftest import (
    FakeCategoryLinker,
    FakeEmoteLinker,
    FakeLinker,
    FakeRoleLinker,
    FakeUserLinker,
    _admin_message,
    _make_ctx,
    _make_sender,
)


# ---------------------------------------------------------------- _is_admin


def test_is_admin_true_with_manage_server():
    sender = _make_sender()
    assert sender._is_admin(_admin_message(manage_server=True)) is True


def test_is_admin_false_without_manage_server():
    sender = _make_sender()
    assert sender._is_admin(_admin_message(manage_server=False)) is False


def test_is_admin_false_when_member_info_is_unavailable():
    sender = _make_sender()
    assert sender._is_admin(SimpleNamespace(channel=FakeChannel(id="c1"))) is False


class _MemberNoPermsCache:
    """Member whose computed `server_permissions` raises (cache miss)."""

    def __init__(self, *, member_id, owner_id):
        self.id = member_id
        self._owner_id = owner_id

    def get_server(self):
        return SimpleNamespace(owner_id=self._owner_id)

    @property
    def server_permissions(self):
        raise RuntimeError("permissions cache miss")


def test_is_admin_true_for_server_owner_when_permissions_unavailable():
    sender = _make_sender()
    member = _MemberNoPermsCache(member_id="owner-1", owner_id="owner-1")
    message = SimpleNamespace(channel=FakeChannel(id="c1"), author_as_member=member)
    assert sender._is_admin(message) is True


def test_is_admin_false_for_non_owner_when_permissions_unavailable():
    sender = _make_sender()
    member = _MemberNoPermsCache(member_id="member-1", owner_id="someone-else")
    message = SimpleNamespace(channel=FakeChannel(id="c1"), author_as_member=member)
    assert sender._is_admin(message) is False


# ---------------------------------------------------------------- shared "needs admin" gate


async def test_each_admin_command_rejects_a_non_admin():
    sender = _make_sender(
        linker=FakeLinker(),
        emote_linker=FakeEmoteLinker(),
        user_linker=FakeUserLinker(),
        category_linker=FakeCategoryLinker(),
        role_linker=FakeRoleLinker(),
    )
    ctx = _make_ctx(manage_server=False)

    await sender._link_channel(ctx, "discord", "s1")
    await sender._link_emote(ctx, "discord", "s1", "l1")
    await sender._link_user(ctx, "discord", "u1", "l1")
    await sender._mirror_channel(ctx)
    await sender._unlink_channel(ctx)
    await sender._unlink_user(ctx)
    await sender._link_category(ctx, "discord", "s1")
    await sender._unlink_category(ctx)
    await sender._link_role(ctx, "Mods", "discord", "111")

    assert ctx.channel.sent == [
        {"content": "You need the Manage Server permission to do that.", "masquerade": None}
    ] * 9


