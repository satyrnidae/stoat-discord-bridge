"""Discord sender/receiver services, split by area of concern.

Instantiated once per configured Discord connector (config.yaml's `discord`
list can have any number of entries) since each guild needs its own
discord.Client/command tree.

Submodules:
  client.py     - the `discord.Client` subclass (event -> owner shim)
  commands.py   - command parsing: the `/link` `/unlink` `/linked` `/mirror` app_commands tree + autocomplete
  linking.py    - the Mongo-backed `_handle_link_*` / `_handle_unlink_*` / `_handle_linked_*` / `_handle_mirror_*` handlers
  lookups.py    - platform-resource lookups (id<->name, get-or-create, category membership)
  sync.py       - reaction / emoji / role / channel-permission sync handlers + coordinator hooks
  formatting.py - network-free conversion helpers (StandardMessage/Reaction, username/emoji sanitising) + _CONTENT_LIMIT
  sender.py     - `DiscordSenderService`: setup/teardown, slash-command sync, inbound relay + thread mirroring
  receiver.py   - `DiscordReceiverService`: outbound relay via per-channel webhook
"""

from stoat_discord_bridge.services.discord_service.client import _DiscordClient
from stoat_discord_bridge.services.discord_service.commands import _connector_autocomplete_choices
from stoat_discord_bridge.services.discord_service.formatting import _CONTENT_LIMIT, _normalize_channel_id
from stoat_discord_bridge.services.discord_service.receiver import DiscordReceiverService
from stoat_discord_bridge.services.discord_service.sender import DiscordSenderService

__all__ = [
    "DiscordSenderService",
    "DiscordReceiverService",
    "_DiscordClient",
    "_connector_autocomplete_choices",
    "_normalize_channel_id",
    "_CONTENT_LIMIT",
]
