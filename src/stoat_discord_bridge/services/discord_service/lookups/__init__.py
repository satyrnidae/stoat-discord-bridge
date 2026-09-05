"""Platform-resource lookups for the Discord connector.

The `ConnectorInfo`-hook half of `DiscordSenderService`: resolving ids to
names and vice versa, get-or-create for roles / categories, and the
Category-membership helpers `/mirror channel` and `/link category` need.
All keyed off `self._client` / `self._config.guild_id`, which the composed
service provides.

Split into one module per concern (issue #92, for architectural consistency
with `stoat_service/lookups/`): `names.py` (plain id<->name resolution),
`channels.py` (channel get-or-create), `categories.py` (Category
get-or-create/membership/move), `roles_emoji.py` (role/emoji get-or-create),
and `listing.py` (the autocomplete `list_*` hooks). No `identity.py` or
`refresh.py` counterpart here - Discord has no local-user-masquerade
resolution to do (that's a Stoat-only concept), and its guild cache is kept
live by gateway events rather than needing a cache-freshness workaround (see
CLAUDE.md). `DiscordLookupsMixin` here composes the sub-mixins back into the
one mixin `DiscordSenderService` expects, so
`from stoat_discord_bridge.services.discord_service.lookups import
DiscordLookupsMixin` keeps working unchanged.
"""

from __future__ import annotations

from stoat_discord_bridge.services.discord_service.lookups.categories import _CategoriesMixin
from stoat_discord_bridge.services.discord_service.lookups.channels import _ChannelsMixin
from stoat_discord_bridge.services.discord_service.lookups.listing import _ListingMixin
from stoat_discord_bridge.services.discord_service.lookups.names import _NamesMixin
from stoat_discord_bridge.services.discord_service.lookups.roles_emoji import _RolesEmojiMixin


class DiscordLookupsMixin(
    _NamesMixin,
    _ChannelsMixin,
    _CategoriesMixin,
    _RolesEmojiMixin,
    _ListingMixin,
):
    """Resource-lookup half of `DiscordSenderService`."""


__all__ = ["DiscordLookupsMixin"]
