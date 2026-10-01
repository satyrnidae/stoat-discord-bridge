"""`reorder_roles` on the Discord connector (issue #199)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from tests.discord_service.conftest import FakeLinker, _make_sender


class _Guild:
    def __init__(self, roles):
        self.roles = list(roles)
        self.position_edits = []

    def get_role(self, role_id):
        return next((r for r in self.roles if r.id == role_id), None)

    async def edit_role_positions(self, positions, **kwargs):
        self.position_edits.append({r.id: p for r, p in positions.items()})


def _sender_with(monkeypatch, guild):
    sender = _make_sender(FakeLinker())
    monkeypatch.setattr(sender, "_guild_or_none", lambda: guild)
    return sender


def _guild():
    # position: higher = higher in Discord's role list.
    return _Guild(
        [
            SimpleNamespace(id=1, position=1),
            SimpleNamespace(id=2, position=2),
            SimpleNamespace(id=3, position=3),
            SimpleNamespace(id=4, position=4),
        ]
    )


async def test_reorder_roles_shuffles_the_given_roles_among_their_own_positions(monkeypatch):
    guild = _guild()
    sender = _sender_with(monkeypatch, guild)

    # 1 and 3 should swap; 2 and 4 aren't named, so they don't move.
    await sender.reorder_roles(["1", "3"])

    assert guild.position_edits == [{1: 3, 3: 1}]


async def test_reorder_roles_is_a_noop_when_already_in_order(monkeypatch):
    guild = _guild()
    sender = _sender_with(monkeypatch, guild)

    await sender.reorder_roles(["4", "2", "1"])

    assert guild.position_edits == []


async def test_reorder_roles_skips_roles_that_arent_cached(monkeypatch):
    guild = _guild()
    sender = _sender_with(monkeypatch, guild)

    await sender.reorder_roles(["1", "99", "2"])

    assert guild.position_edits == [{1: 2, 2: 1}]


async def test_reorder_roles_raises_when_the_guild_isnt_cached(monkeypatch):
    sender = _sender_with(monkeypatch, None)

    with pytest.raises(RuntimeError):
        await sender.reorder_roles(["1", "2"])
