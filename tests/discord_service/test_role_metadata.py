"""`describe_role` / `create_role(metadata=...)` on the Discord connector (issue #179)."""

from __future__ import annotations

from types import SimpleNamespace

import discord

from stoat_discord_bridge.models import RoleMetadata
from tests.discord_service.conftest import FakeLinker, _make_sender


class _Guild:
    def __init__(self, roles=()):
        self.roles = list(roles)
        self.create_calls = []

    def get_role(self, role_id):
        return next((r for r in self.roles if r.id == role_id), None)

    async def create_role(self, **kwargs):
        self.create_calls.append(kwargs)
        role = SimpleNamespace(id=900, name=kwargs["name"])
        self.roles.append(role)
        return role


class _Role(SimpleNamespace):
    async def edit(self, **kwargs):
        self.edits.append(kwargs)


def _role(role_id, name, color=0, hoist=False):
    return _Role(id=role_id, name=name, color=discord.Color(color), hoist=hoist, edits=[])


def _sender_with(monkeypatch, guild):
    sender = _make_sender(FakeLinker())
    monkeypatch.setattr(sender, "_guild_or_none", lambda: guild)
    return sender


async def test_describe_role_reads_color_and_hoist(monkeypatch):
    sender = _sender_with(monkeypatch, _Guild([_role(5, "Mods", color=0xFF8800, hoist=True)]))
    assert await sender.describe_role("5") == RoleMetadata(color="#ff8800", hoist=True)


async def test_describe_role_treats_a_zero_color_as_none(monkeypatch):
    sender = _sender_with(monkeypatch, _Guild([_role(5, "Mods")]))
    assert await sender.describe_role("5") == RoleMetadata(color=None, hoist=False)


async def test_describe_role_returns_none_for_an_unknown_role(monkeypatch):
    sender = _sender_with(monkeypatch, _Guild())
    assert await sender.describe_role("5") is None
    assert await sender.describe_role("not-an-id") is None


async def test_create_role_applies_metadata(monkeypatch):
    guild = _Guild()
    sender = _sender_with(monkeypatch, guild)
    await sender.create_role("Mods", metadata=RoleMetadata(color="#ff8800", hoist=True))
    (call,) = guild.create_calls
    assert call["color"] == discord.Color(0xFF8800)
    assert call["hoist"] is True


async def test_create_role_skips_a_color_discord_cant_take(monkeypatch):
    # A Stoat source's color can be any CSS value, e.g. a gradient.
    guild = _Guild()
    sender = _sender_with(monkeypatch, guild)
    await sender.create_role("Mods", metadata=RoleMetadata(color="linear-gradient(red, blue)", hoist=True))
    (call,) = guild.create_calls
    assert "color" not in call
    assert call["hoist"] is True


async def test_create_role_without_metadata_creates_by_name_only(monkeypatch):
    guild = _Guild()
    sender = _sender_with(monkeypatch, guild)
    await sender.create_role("Mods")
    (call,) = guild.create_calls
    assert "color" not in call and "hoist" not in call


async def test_apply_role_metadata_syncs_a_differing_role(monkeypatch):
    # issue #195: a matched role is edited to match the source.
    role = _role(5, "Mods", color=0x00FF00, hoist=False)
    sender = _sender_with(monkeypatch, _Guild([role]))
    await sender.apply_role_metadata("5", RoleMetadata(color="#ff8800", hoist=True))
    assert role.edits == [{"reason": "bridge role mirror", "color": discord.Color(0xFF8800), "hoist": True}]


async def test_apply_role_metadata_clears_a_color_the_source_lacks(monkeypatch):
    role = _role(5, "Mods", color=0x00FF00, hoist=True)
    sender = _sender_with(monkeypatch, _Guild([role]))
    await sender.apply_role_metadata("5", RoleMetadata())
    assert role.edits == [{"reason": "bridge role mirror", "color": discord.Color(0), "hoist": False}]


async def test_apply_role_metadata_leaves_the_color_for_one_discord_cant_take(monkeypatch):
    role = _role(5, "Mods", color=0x00FF00, hoist=True)
    sender = _sender_with(monkeypatch, _Guild([role]))
    await sender.apply_role_metadata("5", RoleMetadata(color="linear-gradient(red, blue)", hoist=True))
    assert role.edits == []


async def test_apply_role_metadata_is_a_noop_when_already_matching(monkeypatch):
    role = _role(5, "Mods", color=0xFF8800, hoist=True)
    sender = _sender_with(monkeypatch, _Guild([role]))
    await sender.apply_role_metadata("5", RoleMetadata(color="#ff8800", hoist=True))
    assert role.edits == []


async def test_create_role_creates_even_if_a_same_named_role_exists(monkeypatch):
    # Matching by name is resolve_role_id_by_name's job now (issue #183).
    guild = _Guild([_role(5, "Mods")])
    sender = _sender_with(monkeypatch, guild)
    assert await sender.create_role("mods") == "900"
    assert [c["name"] for c in guild.create_calls] == ["mods"]
