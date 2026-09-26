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


def test_mention_everyone_round_trips():
    neutral = RolePermissionOverride(allow=frozenset({"mention_everyone"}), deny=frozenset())
    a, d = neutral_to_discord_pair(neutral, _Perms)
    assert a.mention_everyone is True
    assert discord_overwrite_to_neutral(a, d) == neutral
    a, d = neutral_to_stoat_pair(neutral, _Perms)
    assert a.mention_everyone is True
    assert stoat_override_to_neutral(a, d) == neutral


def test_video_maps_to_discord_stream():
    """The one new entry whose attr names differ between platforms."""
    neutral = RolePermissionOverride(allow=frozenset({"video"}), deny=frozenset())
    a, d = neutral_to_discord_pair(neutral, _Perms)
    assert a.stream is True and a.video is False
    assert discord_overwrite_to_neutral(a, d) == neutral
    a, d = neutral_to_stoat_pair(neutral, _Perms)
    assert a.video is True and a.stream is False
    assert stoat_override_to_neutral(a, d) == neutral


def test_voice_bits_round_trip():
    neutral = RolePermissionOverride(
        allow=frozenset({"speak", "move_members"}),
        deny=frozenset({"mute_members", "deafen_members"}),
    )
    a, d = neutral_to_discord_pair(neutral, _Perms)
    assert a.speak is True and a.move_members is True
    assert d.mute_members is True and d.deafen_members is True
    assert discord_overwrite_to_neutral(a, d) == neutral
    a, d = neutral_to_stoat_pair(neutral, _Perms)
    assert stoat_override_to_neutral(a, d) == neutral


def test_connect_also_sets_stoat_listen():
    """Discord's one connect bit covers what Stoat splits into connect and
    listen, so writing it to Stoat sets both."""
    a, d = neutral_to_stoat_pair(RolePermissionOverride(allow=frozenset({"connect"}), deny=frozenset()), _Perms)
    assert a.connect is True and a.listen is True
    assert d.connect is False and d.listen is False

    a, d = neutral_to_stoat_pair(RolePermissionOverride(allow=frozenset(), deny=frozenset({"connect"})), _Perms)
    assert d.connect is True and d.listen is True
    assert a.connect is False and a.listen is False


def test_connect_does_not_set_discord_listen():
    neutral = RolePermissionOverride(allow=frozenset({"connect"}), deny=frozenset())
    a, _ = neutral_to_discord_pair(neutral, _Perms)
    assert a.connect is True and a.listen is False


def test_stoat_listen_is_ignored_on_read():
    """listen is write-only: reads look at Stoat's connect bit alone."""
    assert stoat_override_to_neutral(_Perms(connect=True, listen=False), _Perms()).allow == frozenset({"connect"})
    assert stoat_override_to_neutral(_Perms(listen=True), _Perms()).allow == frozenset()


def test_splice_preserves_unmapped_target_bits():
    base = RolePermissionOverride(
        allow=frozenset({"use_masquerade", "view_channel"}),  # use_masquerade is unmapped
        deny=frozenset({"mention_roles"}),  # mention_roles is unmapped
    )
    incoming = RolePermissionOverride(allow=frozenset(), deny=frozenset({"view_channel"}))
    out = incoming.splice_onto(base)
    assert "use_masquerade" in out.allow  # unmapped bit kept
    assert "mention_roles" in out.deny  # unmapped bit kept
    assert "view_channel" in out.deny  # mapped bit taken from incoming
    assert "view_channel" not in out.allow


def test_splice_onto_none():
    incoming = RolePermissionOverride(allow=frozenset({"send_messages"}), deny=frozenset())
    out = incoming.splice_onto(None)
    assert out == incoming


def test_every_neutral_name_has_two_targets():
    for name, pair in NEUTRAL_PERMISSIONS.items():
        assert len(pair) == 2 and all(pair)
