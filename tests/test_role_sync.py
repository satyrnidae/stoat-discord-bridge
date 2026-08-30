from dataclasses import dataclass

import pytest

from stoat_discord_bridge.services.role_sync import (
    NEUTRAL_PERMISSIONS,
    RolePermissionOverride,
    discord_overwrite_to_neutral,
    neutral_to_discord_pair,
    neutral_to_stoat_pair,
    role_id_set_diff,
    stoat_override_to_neutral,
)


class _Perms:
    """Stand-in for discord.Permissions / stoat.Permissions: attribute bag
    with a .none() classmethod."""

    def __init__(self, **flags):
        self._flags = dict(flags)

    def __getattr__(self, name):
        return self._flags.get(name, False)

    def __setattr__(self, name, value):
        if name == "_flags":
            super().__setattr__(name, value)
        else:
            self._flags[name] = value

    @classmethod
    def none(cls):
        return cls()


def test_role_id_set_diff():
    added, removed = role_id_set_diff({"a", "b"}, {"b", "c"})
    assert added == {"c"}
    assert removed == {"a"}
    assert role_id_set_diff({"a"}, {"a"}) == (set(), set())


def test_override_rejects_contradiction():
    with pytest.raises(ValueError):
        RolePermissionOverride(allow=frozenset({"send_messages"}), deny=frozenset({"send_messages"}))


def test_discord_overwrite_round_trips_mapped_bits_only():
    allow = _Perms(send_messages=True, ban_members=True)  # ban_members is unmapped
    deny = _Perms(view_channel=True)
    neutral = discord_overwrite_to_neutral(allow, deny)
    assert neutral.allow == frozenset({"send_messages"})
    assert neutral.deny == frozenset({"view_channel"})

    a, d = neutral_to_discord_pair(neutral, _Perms)
    assert a.send_messages is True and a.view_channel is False
    assert d.view_channel is True


def test_added_neutral_bits_map_to_stoat_attrs():
    neutral = RolePermissionOverride(
        allow=frozenset({"embed_links", "attach_files"}),
        deny=frozenset({"add_reactions"}),
    )
    a, d = neutral_to_stoat_pair(neutral, _Perms)
    assert a.send_embeds is True and a.upload_files is True
    assert d.react is True
    # and back
    assert stoat_override_to_neutral(a, d) == neutral


def test_stoat_override_round_trips():
    allow = _Perms(view_channel=True)
    deny = _Perms(send_messages=True)
    neutral = stoat_override_to_neutral(allow, deny)
    assert neutral.allow == frozenset({"view_channel"})
    assert neutral.deny == frozenset({"send_messages"})
    a, d = neutral_to_stoat_pair(neutral, _Perms)
    assert a.view_channel is True
    assert d.send_messages is True


def test_splice_preserves_unmapped_target_bits():
    base = RolePermissionOverride(
        allow=frozenset({"connect", "view_channel"}),  # connect is unmapped
        deny=frozenset({"speak"}),  # speak is unmapped
    )
    incoming = RolePermissionOverride(allow=frozenset(), deny=frozenset({"view_channel"}))
    out = incoming.splice_onto(base)
    assert "connect" in out.allow  # unmapped bit kept
    assert "speak" in out.deny  # unmapped bit kept
    assert "view_channel" in out.deny  # mapped bit taken from incoming
    assert "view_channel" not in out.allow


def test_splice_onto_none():
    incoming = RolePermissionOverride(allow=frozenset({"send_messages"}), deny=frozenset())
    out = incoming.splice_onto(None)
    assert out == incoming


def test_every_neutral_name_has_two_targets():
    for name, pair in NEUTRAL_PERMISSIONS.items():
        assert len(pair) == 2 and all(pair)
