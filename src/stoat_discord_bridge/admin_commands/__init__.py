"""Shared logic behind the `/link` `/unlink` `/mirror` `/linked` admin
commands, called identically from each connector's own command handler
(services/discord_service.py, stoat_service.py, irc_service.py) so the
bridge-group/conflict logic isn't duplicated three times.

Split into one module per linker (issue #90): `common.py` holds the shared
hook/error/parse primitives (`ConnectorInfo`, `LinkError`, `MirrorGuard`,
`pop_kv_option`, ...) and `channel.py` / `category.py` / `emote.py` /
`user.py` / `role.py` each hold one linker class - `ChannelLinker` /
`CategoryLinker` / `EmoteLinker` / `UserLinker` / `RoleLinker` respectively.
This `__init__.py` re-exports every public name so every existing
`from stoat_discord_bridge.admin_commands import <name>` call site keeps
working unchanged.
"""

from stoat_discord_bridge.admin_commands.category import CategoryLinker
from stoat_discord_bridge.admin_commands.channel import ChannelLinker
from stoat_discord_bridge.admin_commands.common import (
    ConnectorInfo,
    LinkError,
    MirrorGuard,
    MirrorInProgressError,
    pop_kv_option,
)
from stoat_discord_bridge.admin_commands.emote import EmoteLinker
from stoat_discord_bridge.admin_commands.role import RoleLinker
from stoat_discord_bridge.admin_commands.user import UserLinker

__all__ = [
    "CategoryLinker",
    "ChannelLinker",
    "ConnectorInfo",
    "EmoteLinker",
    "LinkError",
    "MirrorGuard",
    "MirrorInProgressError",
    "RoleLinker",
    "UserLinker",
    "pop_kv_option",
]
