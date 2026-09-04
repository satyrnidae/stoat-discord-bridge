"""Platform-resource lookups for the Stoat connector.

The `ConnectorInfo`-hook half of `StoatSenderService`: resolving ids to
names and vice versa, get-or-create for channels / roles / categories /
emoji, and the Category-placement plumbing (`/mirror channel`, Discord
thread mirroring). All keyed off `self.server_id` / `self._client` /
`self._category_linker`, which the composed service provides.

Split into one module per concern (issue #92, the source-side counterpart of
#90's `admin_commands` split): `names.py` (plain id<->name resolution),
`identity.py` (`get_masquerade_identity`), `channels.py` (channel
get-or-create), `categories.py` (Category placement - the largest single
piece, split further from the #66/#81 cache-refresh machinery in
`refresh.py` so neither module grows past discord's own `lookups.py`),
`roles_emoji.py` (role/emoji get-or-create), and `listing.py` (the
autocomplete `list_*` hooks). `StoatLookupsMixin` here composes them back
into the one mixin `StoatSenderService` expects, so
`from stoat_discord_bridge.services.stoat_service.lookups import
StoatLookupsMixin` keeps working unchanged.
"""

from __future__ import annotations

from stoat_discord_bridge.services.stoat_service.lookups.categories import _CategoriesMixin
from stoat_discord_bridge.services.stoat_service.lookups.channels import _ChannelsMixin
from stoat_discord_bridge.services.stoat_service.lookups.identity import _IdentityMixin
from stoat_discord_bridge.services.stoat_service.lookups.listing import _ListingMixin
from stoat_discord_bridge.services.stoat_service.lookups.names import _NamesMixin
from stoat_discord_bridge.services.stoat_service.lookups.refresh import _RefreshMixin
from stoat_discord_bridge.services.stoat_service.lookups.roles_emoji import _RolesEmojiMixin


class StoatLookupsMixin(
    _NamesMixin,
    _IdentityMixin,
    _ChannelsMixin,
    _CategoriesMixin,
    _RefreshMixin,
    _RolesEmojiMixin,
    _ListingMixin,
):
    """Resource-lookup half of `StoatSenderService`."""


__all__ = ["StoatLookupsMixin"]
