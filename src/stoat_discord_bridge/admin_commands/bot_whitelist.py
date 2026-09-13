"""`BotWhitelistManager` - `/whitelist add|remove`, `/whitelisted` (issue
#120). Lets specific bot users' messages/edits/reactions relay through the
bridge like any human's, instead of being dropped everywhere by the
`author.bot` filters.

Not a bridge-group linker - a whitelist entry has no cross-connector "link",
just a per-(connector, bot user) allow flag - so this is closer in shape to
a lookup plus a couple of mutating verbs than to `UserLinker` et al., though
it's modelled on `UserLinker` for the name<->id resolution and error-handling
conventions (`LinkError`, `_resolve_entity_id`/`_resolve_entity_title`).

Discord/Stoat only - IRC has no bot concept and already relays every nick,
so it has no `ConnectorInfo` entries plugged in here at all (callers just
never route an IRC connector id through this class).
"""

from __future__ import annotations

from stoat_discord_bridge.admin_commands.common import (
    ConnectorInfo,
    LinkError,
    _require_known_connector,
    _resolve_entity_id,
    _resolve_entity_title,
)
from stoat_discord_bridge.admin_commands.user import _strip_discord_mention
from stoat_discord_bridge.services.caching import AsyncTTLCache
from stoat_discord_bridge.storage.bot_whitelist import BotWhitelistEntry, BotWhitelistRepository
from stoat_discord_bridge.storage.user_mappings import UserMappingRepository

# Keeps the two Mongo reads (`is_whitelisted` + linked-group lookup) off the
# hot per-bot-message path; short enough that add/remove still take effect
# quickly for anyone not explicitly invalidated below.
_CACHE_TTL = 60.0


def _cache_key(connector_id: str, user_id: str) -> str:
    return f"{connector_id}:{user_id}"


class BotWhitelistManager:
    def __init__(
        self,
        repo: BotWhitelistRepository,
        user_mappings: UserMappingRepository,
        connectors: dict[str, ConnectorInfo],
        seed: frozenset[tuple[str, str]] = frozenset(),
    ) -> None:
        self._repo = repo
        self._user_mappings = user_mappings
        self._connectors = connectors
        self._seed = seed
        self._cache: AsyncTTLCache[bool] = AsyncTTLCache(_CACHE_TTL)

    @property
    def connectors(self) -> dict[str, ConnectorInfo]:
        return self._connectors

    async def is_whitelisted(self, connector_id: str, user_id: str) -> bool:
        """The single call every sender's bot-authored-event path uses. True
        if `user_id` on `connector_id` is directly whitelisted (the
        `config.yaml` seed, or a runtime `/whitelist add` entry), or is
        `/link user`-linked to an identity that is - so whitelisting a bot on
        one connector also re-admits its linked copy elsewhere."""
        if (connector_id, user_id) in self._seed:
            return True
        return await self._cache.get(_cache_key(connector_id, user_id), self._is_whitelisted_uncached)

    async def _is_whitelisted_uncached(self, key: str) -> bool:
        connector_id, _, user_id = key.partition(":")
        if await self._repo.is_whitelisted(connector_id, user_id):
            return True
        link_group = await self._user_mappings.get_link_group(connector_id, user_id)
        if link_group is None:
            return False
        for mapping in await self._user_mappings.get_mapped_users(link_group):
            if (mapping.connector_id, mapping.user_id) in self._seed:
                return True
            if await self._repo.is_whitelisted(mapping.connector_id, mapping.user_id):
                return True
        return False

    async def whitelist_bot(self, *, target_connector: str, bot_ref: str, added_by: str | None = None) -> str:
        """`/whitelist add [local|<service>] <bot_id|name>`. `target_connector`
        is already resolved from `local` to the invoking connector id by the
        front end. Raises LinkError if `target_connector` is unknown, the
        resolved id is the bridge's own bot on that connector, or it's
        already whitelisted (directly - a linked-in whitelist elsewhere
        doesn't block adding a direct entry here too)."""
        _require_known_connector(self._connectors, target_connector)
        bot_id = await self._resolve_to_id(target_connector, _strip_discord_mention(bot_ref))
        info = self._connectors[target_connector]
        self_id = info.self_user_id() if info.self_user_id is not None else None
        if self_id is not None and bot_id == self_id:
            raise LinkError("can't whitelist the bridge's own bot.")
        label = await self._resolve_name(target_connector, bot_id)
        added = await self._repo.add(
            BotWhitelistEntry(connector_id=target_connector, user_id=bot_id, label=label, added_by=added_by)
        )
        self._cache.invalidate(_cache_key(target_connector, bot_id))
        if not added:
            raise LinkError("already whitelisted.")
        return f"Whitelisted bot '{label or bot_id}' on {info.label}."

    async def remove_bot(self, *, target_connector: str, bot_ref: str) -> str:
        """`/whitelist remove [local|<service>] <bot_id|name>`. Raises
        LinkError if `target_connector` is unknown, the resolved id is
        config-pinned (in `whitelisted_bots:`, not removable here), or it
        wasn't whitelisted at all."""
        _require_known_connector(self._connectors, target_connector)
        bot_id = await self._resolve_to_id(target_connector, _strip_discord_mention(bot_ref))
        if (target_connector, bot_id) in self._seed:
            raise LinkError("that bot is pinned in config.yaml; edit whitelisted_bots to remove it.")
        removed = await self._repo.remove(target_connector, bot_id)
        self._cache.invalidate(_cache_key(target_connector, bot_id))
        if not removed:
            raise LinkError("wasn't whitelisted.")
        info = self._connectors.get(target_connector)
        label = info.label if info is not None else target_connector
        return f"Removed bot '{bot_id}' from the {label} whitelist."

    async def list_whitelisted_bots(self, *, target_connector: str) -> str:
        """`/whitelisted [local|<service>]` - read-only. Seed entries first
        (marked `(config)`), then runtime Mongo entries, each rendered as
        `<label or a fresh resolve_user_name> (<id>)`."""
        _require_known_connector(self._connectors, target_connector)
        info = self._connectors[target_connector]
        lines: list[str] = []
        for connector_id, user_id in sorted(self._seed):
            if connector_id != target_connector:
                continue
            label = await self._resolve_name(target_connector, user_id) or user_id
            lines.append(f"{label} ({user_id}) (config)")
        for entry in await self._repo.list_for_connector(target_connector):
            label = entry.label or await self._resolve_name(target_connector, entry.user_id) or entry.user_id
            lines.append(f"{label} ({entry.user_id})")
        if not lines:
            return f"No bots are whitelisted on {info.label}."
        return f"Whitelisted bots on {info.label}:\n" + "\n".join(lines)

    async def _resolve_to_id(self, connector: str, token: str) -> str:
        info = self._connectors.get(connector)
        hook = info.resolve_user_id_by_name if info else None
        return await _resolve_entity_id(token, hook, connector=connector, kind="user")

    async def _resolve_name(self, connector: str, user_id: str) -> str | None:
        info = self._connectors.get(connector)
        hook = info.resolve_user_name if info else None
        return await _resolve_entity_title(user_id, hook, connector=connector, kind="user")
