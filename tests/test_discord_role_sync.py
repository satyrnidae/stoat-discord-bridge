"""DiscordSenderService role auto-grant helpers: _handle_member_update diff
and grant_role/revoke_role idempotency. Built via object.__new__ - these
helpers don't need the client the constructor builds."""

from __future__ import annotations

from types import SimpleNamespace

from stoat_discord_bridge.services.discord_service import DiscordSenderService


def _sender(*, on_change=None, client=None):
    s = object.__new__(DiscordSenderService)
    s.connector_id = "discord"
    s._config = SimpleNamespace(guild_id=123)
    s._on_member_roles_changed = on_change
    if client is not None:
        s._client = client
    return s


def _member(guild_id, role_ids):
    return SimpleNamespace(
        guild=SimpleNamespace(id=guild_id),
        id=7,
        roles=[SimpleNamespace(id=r) for r in role_ids],
    )


async def test_handle_member_update_reports_added_and_removed():
    seen = []

    async def on_change(connector_id, user_id, added, removed):
        seen.append((connector_id, user_id, added, removed))

    s = _sender(on_change=on_change)
    await s._handle_member_update(_member(123, [1, 2]), _member(123, [2, 3]))
    assert seen == [("discord", "7", {"3"}, {"1"})]


async def test_handle_member_update_noop_when_roles_unchanged():
    seen = []

    async def on_change(*a):
        seen.append(a)

    s = _sender(on_change=on_change)
    await s._handle_member_update(_member(123, [1, 2]), _member(123, [2, 1]))
    assert seen == []


async def test_handle_member_update_ignores_other_guilds():
    seen = []

    async def on_change(*a):
        seen.append(a)

    s = _sender(on_change=on_change)
    await s._handle_member_update(_member(999, [1]), _member(999, [1, 2]))
    assert seen == []


class _FakeGuild:
    def __init__(self):
        self.role = SimpleNamespace(id=50)
        self.member_roles: list = []
        self.added: list = []
        self.removed: list = []
        parent = self

        class _M:
            @property
            def roles(self_inner):
                return parent.member_roles

            async def add_roles(self_inner, role, reason=None):
                parent.member_roles.append(role)
                parent.added.append(role.id)

            async def remove_roles(self_inner, role, reason=None):
                parent.member_roles.remove(role)
                parent.removed.append(role.id)

        self.member = _M()

    def get_member(self, _):
        return self.member

    def get_role(self, rid):
        return self.role if rid == 50 else None


async def test_grant_role_idempotent():
    guild = _FakeGuild()
    s = _sender(client=SimpleNamespace(get_guild=lambda _: guild))
    await s.grant_role("7", "50")
    assert guild.added == [50]
    await s.grant_role("7", "50")  # already has it - no second call
    assert guild.added == [50]
    await s.revoke_role("7", "50")
    assert guild.removed == [50]
    await s.revoke_role("7", "50")  # already gone
    assert guild.removed == [50]
