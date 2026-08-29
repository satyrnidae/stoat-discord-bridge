"""Network-free helpers for the role auto-grant and per-channel
permission-mirror flows (see bridge.py's RoleGrantCoordinator and each
service's role hooks).

Kept separate from the service modules so it's unit-testable without a live
client.

Permission mirroring is deliberately conservative: only the handful of
permission bits whose meaning is the same on Discord and Stoat are
translated (NEUTRAL_PERMISSIONS below). Everything else on the target's
existing override is left untouched - callers read the current override,
splice in the mapped bits, and write it back.
"""

from __future__ import annotations

from dataclasses import dataclass

# neutral name -> (discord.Permissions attr, stoat Permissions attr). Only
# bits that mean the same thing on both platforms. TODO: the stoat.py flag
# names are a best guess against the Revolt lineage - verify against a live
# server, same caveat as the rest of the Stoat integration.
NEUTRAL_PERMISSIONS: dict[str, tuple[str, str]] = {
    "view_channel": ("view_channel", "view_channel"),
    "read_message_history": ("read_message_history", "read_message_history"),
    "send_messages": ("send_messages", "send_messages"),
    "manage_messages": ("manage_messages", "manage_messages"),
    "manage_channel": ("manage_channels", "manage_channels"),
    "manage_webhooks": ("manage_webhooks", "manage_webhooks"),
    "create_invites": ("create_instant_invite", "create_invites"),
    "manage_permissions": ("manage_permissions", "manage_permissions"),
}


@dataclass(frozen=True)
class RolePermissionOverride:
    """A role's permission override on one channel, as neutral permission
    names. `allow` and `deny` are disjoint; a name in neither is "unset"
    (inherit)."""

    allow: frozenset[str]
    deny: frozenset[str]

    def __post_init__(self) -> None:
        overlap = self.allow & self.deny
        if overlap:
            raise ValueError(f"permission(s) both allowed and denied: {sorted(overlap)}")

    def splice_onto(self, base: "RolePermissionOverride | None") -> "RolePermissionOverride":
        """Return `base` with every mapped (neutral) permission replaced by
        this override's value for it, and every unmapped permission left as
        `base` had it. Used so mirroring only ever touches the shared bit
        subset."""
        mapped = set(NEUTRAL_PERMISSIONS)
        base_allow = (base.allow if base else frozenset()) - mapped
        base_deny = (base.deny if base else frozenset()) - mapped
        return RolePermissionOverride(
            allow=frozenset(base_allow | (self.allow & mapped)),
            deny=frozenset(base_deny | (self.deny & mapped)),
        )


def role_id_set_diff(before: set[str], after: set[str]) -> tuple[set[str], set[str]]:
    """(added, removed) role ids between two snapshots of a member's roles."""
    return after - before, before - after


def discord_overwrite_to_neutral(allow, deny) -> RolePermissionOverride:
    """`allow`/`deny` are `discord.Permissions` (from
    `PermissionOverwrite.pair()`). Only mapped bits are carried over."""
    a = {name for name, (d_attr, _) in NEUTRAL_PERMISSIONS.items() if getattr(allow, d_attr, False)}
    r = {name for name, (d_attr, _) in NEUTRAL_PERMISSIONS.items() if getattr(deny, d_attr, False)}
    return RolePermissionOverride(allow=frozenset(a), deny=frozenset(r))


def neutral_to_discord_pair(override: RolePermissionOverride, permissions_cls):
    """-> (allow, deny) as `permissions_cls` instances, for building a
    `discord.PermissionOverwrite`. Only mapped bits are set."""
    allow = permissions_cls.none()
    deny = permissions_cls.none()
    for name in override.allow:
        d_attr = NEUTRAL_PERMISSIONS[name][0]
        setattr(allow, d_attr, True)
    for name in override.deny:
        d_attr = NEUTRAL_PERMISSIONS[name][0]
        setattr(deny, d_attr, True)
    return allow, deny


def stoat_override_to_neutral(allow, deny) -> RolePermissionOverride:
    """`allow`/`deny` are stoat `Permissions` bitfields (a channel's
    `PermissionOverride` for a role). Only mapped bits are carried over."""
    a = {name for name, (_, s_attr) in NEUTRAL_PERMISSIONS.items() if getattr(allow, s_attr, False)}
    r = {name for name, (_, s_attr) in NEUTRAL_PERMISSIONS.items() if getattr(deny, s_attr, False)}
    return RolePermissionOverride(allow=frozenset(a), deny=frozenset(r))


def neutral_to_stoat_pair(override: RolePermissionOverride, permissions_cls):
    """-> (allow, deny) as stoat `Permissions` instances. Only mapped bits
    are set."""
    allow = permissions_cls.none()
    deny = permissions_cls.none()
    for name in override.allow:
        s_attr = NEUTRAL_PERMISSIONS[name][1]
        setattr(allow, s_attr, True)
    for name in override.deny:
        s_attr = NEUTRAL_PERMISSIONS[name][1]
        setattr(deny, s_attr, True)
    return allow, deny
