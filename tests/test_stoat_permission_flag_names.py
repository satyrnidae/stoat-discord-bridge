"""Spike (#6): pin every permission flag name the bridge references against
the *real* ``stoat.Permissions`` / ``discord.Permissions`` flag classes.

The rest of ``test_role_sync`` exercises the translation logic through a
hand-rolled ``_Perms`` bag, so a typo'd attr name (``manage_channel`` vs
``manage_channels``, ``add_reactions`` vs ``react``) would sail through
there. These tests fail loudly if either library renames or drops a flag
the bridge binds to.

Verified against stoat.py 1.2.1 / discord.py 2.7.1 - all names were already
correct.
"""

import discord
import stoat

from stoat_discord_bridge.services.role_sync import NEUTRAL_PERMISSIONS


def _has_flag(perms_cls, name: str) -> bool:
    """A flag class exposes each flag as a class-level descriptor; an
    instance from ``.none()`` reads it as a bool."""
    return hasattr(perms_cls, name) and isinstance(getattr(perms_cls.none(), name), bool)


def test_neutral_permissions_discord_attrs_exist():
    missing = [
        (name, d_attr)
        for name, (d_attr, _) in NEUTRAL_PERMISSIONS.items()
        if not _has_flag(discord.Permissions, d_attr)
    ]
    assert not missing, f"discord.Permissions has no such flag(s): {missing}"


def test_neutral_permissions_stoat_attrs_exist():
    missing = [
        (name, s_attr)
        for name, (_, s_attr) in NEUTRAL_PERMISSIONS.items()
        if not _has_flag(stoat.Permissions, s_attr)
    ]
    assert not missing, f"stoat.Permissions has no such flag(s): {missing}"


def test_stoat_manage_server_flag_exists():
    """Backs ``StoatSenderService._is_admin`` - the command-execution gate."""
    assert _has_flag(stoat.Permissions, "manage_server")


def test_stoat_permissions_none_is_settable_and_readable():
    """``neutral_to_stoat_pair`` does ``Permissions.none()`` then
    ``setattr(p, attr, True)`` and later ``getattr(p, attr, False)``."""
    p = stoat.Permissions.none()
    assert p.react is False
    p.react = True
    p.send_embeds = True
    assert p.react is True and p.send_embeds is True
    assert p.view_channel is False


def test_stoat_permission_override_exposes_allow_deny_as_permissions():
    """``stoat_override_to_neutral`` / ``get_channel_role_permission`` read
    ``override.allow`` / ``.deny`` and expect ``Permissions`` instances."""
    override = stoat.PermissionOverride(
        allow=stoat.Permissions(send_messages=True),
        deny=stoat.Permissions(view_channel=True),
    )
    assert isinstance(override.allow, stoat.Permissions)
    assert isinstance(override.deny, stoat.Permissions)
    assert override.allow.send_messages is True
    assert override.deny.view_channel is True
