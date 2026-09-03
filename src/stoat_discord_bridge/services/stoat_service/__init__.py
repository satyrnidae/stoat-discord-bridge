"""Stoat sender/receiver services, split by area of concern.

Instantiated once per configured Stoat connector (config.yaml's `stoat`
list can have any number of entries - public, self-hosted, or more) since
each Stoat deployment needs its own client/session.

Submodules:
  discovery.py  - deployment websocket/CDN URL discovery (setup)
  client.py     - the `stoat.ext.commands.Bot` subclass (event -> owner shim)
  commands.py   - command parsing: the `/link` `/unlink` `/linked` `/mirror` tree + help text
  linking.py    - the Mongo-backed `_link_*` / `_unlink_*` / `_linked_*` / `_mirror_*` handlers + admin gate
  lookups.py    - platform-resource lookups (id<->name, get-or-create, category placement)
  sync.py       - reaction / emoji / role / typing / channel sync event handlers + coordinator hooks
  formatting.py - network-free conversion helpers (display name, avatar, attachments, emoji parsing)
  sender.py     - `StoatSenderService`: setup/teardown + inbound message relay
  receiver.py   - `StoatReceiverService`: outbound relay via masquerade
"""

from stoat_discord_bridge.services.stoat_service.client import _StoatClient
from stoat_discord_bridge.services.stoat_service.commands import _help_text
from stoat_discord_bridge.services.stoat_service.discovery import (
    _discover_cdn_base,
    _discover_node_config,
    _discover_websocket_base,
)
from stoat_discord_bridge.services.stoat_service.formatting import (
    _CONTENT_LIMIT,
    _avatar_url,
    _display_name,
    _parse_stoat_emoji,
)
from stoat_discord_bridge.services.stoat_service.receiver import StoatReceiverService
from stoat_discord_bridge.services.stoat_service.sender import StoatSenderService

__all__ = [
    "StoatSenderService",
    "StoatReceiverService",
    "_StoatClient",
    "_help_text",
    "_discover_node_config",
    "_discover_websocket_base",
    "_discover_cdn_base",
    "_avatar_url",
    "_display_name",
    "_parse_stoat_emoji",
    "_CONTENT_LIMIT",
]
