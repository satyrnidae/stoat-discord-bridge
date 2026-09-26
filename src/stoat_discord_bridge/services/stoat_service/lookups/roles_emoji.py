"""Role and custom-emoji get-or-create / resolve for the Stoat connector -
`create_role` and the emoji trio (`_all_emojis` / `resolve_emoji` /
`resolve_emoji_id_by_name` / `get_emoji_name`) behind `/mirror role` and
`/mirror emote`. Plain role-name resolution (`get_role_name` /
`resolve_role_id_by_name`) lives alongside the rest of the id<->name lookups
in `names.py`.
"""

from __future__ import annotations

import logging

import stoat

from stoat_discord_bridge.models import CustomEmoji, RoleMetadata

logger = logging.getLogger(__name__)


class _RolesEmojiMixin:
    """Role/emoji get-or-create half of `StoatLookupsMixin`."""

    async def create_role(self, name: str, *, metadata: "RoleMetadata | None" = None) -> str:
        """Create a new role named `name`, returning its id - this
        connector's `ConnectorInfo.create_role` for `/mirror role`. Never
        matches an existing role; that's `resolve_role_id_by_name`'s job
        (issue #183).

        `metadata`'s color/hoist are applied too (issue #179). stoat.py's
        `create_role` takes only a name, so they're set by a follow-up
        `Role.edit`; if that fails the role is still linked, just
        uncolored."""
        server = self._client.get_server(self.server_id, partial=False)
        if not isinstance(server, stoat.Server):
            server = await self._client.fetch_server(self.server_id)
        role = await server.create_role(name=name)
        if metadata is not None and (metadata.color or metadata.hoist):
            try:
                await role.edit(color=metadata.color, hoist=metadata.hoist)
            except Exception:
                logger.warning(
                    "[stoat:%s] couldn't set color/hoist on new role %r", self.connector_id, name, exc_info=True
                )
        return str(role.id)

    async def describe_role(self, role_id: str) -> "RoleMetadata | None":
        """A role's color/hoist, this connector's `ConnectorInfo.describe_role`
        (issue #179). `Role.color` is already a CSS string, passed as-is.
        None if the role isn't cached."""
        try:
            role = self._role_by_id(role_id)
        except Exception:
            return None
        if role is None:
            return None
        return RoleMetadata(color=getattr(role, "color", None) or None, hoist=bool(getattr(role, "hoist", False)))

    async def _all_emojis(self) -> list:
        """Every custom emoji on this server - `server.emojis` if the cache
        has it, else a REST fetch. `Server.emojis` is a Mapping[id, emoji],
        so iterate its `.values()`, not the mapping itself (which yields ids)."""
        try:
            server = self._client.get_server(self.server_id, partial=True)
            emojis = getattr(server, "emojis", None) or {}
        except Exception:
            emojis = {}
        values = list(getattr(emojis, "values", lambda: emojis)())
        if values:
            return values
        try:
            server = await self._full_server()
            return list(await server.fetch_emojis())
        except Exception:
            logger.debug("[stoat:%s] fetch_emojis failed", self.connector_id, exc_info=True)
            return []

    async def _full_server(self):
        server = self._client.get_server(self.server_id, partial=False)
        if not isinstance(server, stoat.Server):
            server = await self._client.fetch_server(self.server_id)
        return server

    async def get_emoji_name(self, emoji_id: str) -> str | None:
        """Best-effort emoji-id -> name lookup, this connector's
        `ConnectorInfo.resolve_emoji_name`."""
        try:
            server = self._client.get_server(self.server_id, partial=True)
            emoji = server.get_emoji(emoji_id)
        except Exception:
            emoji = None
        if emoji is None:
            emoji = next((e for e in await self._all_emojis() if str(e.id) == emoji_id), None)
        return getattr(emoji, "name", None) if emoji is not None else None

    async def resolve_emoji_id_by_name(self, token: str) -> str | None:
        """Resolve a bare custom-emoji name to its id (case-insensitive, first
        match); a token that's already an emoji id is returned as-is, an
        unknown token yields None - this connector's
        `ConnectorInfo.resolve_emoji_id_by_name`."""
        emojis = await self._all_emojis()
        if any(str(getattr(e, "id", "")) == token for e in emojis):
            return token
        lowered = token.casefold()
        for e in emojis:
            if str(getattr(e, "name", "")).casefold() == lowered:
                return str(e.id)
        return None

    async def resolve_emoji(self, emoji_id: str) -> "CustomEmoji | None":
        """emoji-id -> full CustomEmoji, this connector's
        `ConnectorInfo.resolve_emoji` (the source side of `/mirror emote`)."""
        try:
            server = self._client.get_server(self.server_id, partial=True)
            emoji = server.get_emoji(emoji_id)
        except Exception:
            emoji = None
        if emoji is None:
            emoji = next((e for e in await self._all_emojis() if str(e.id) == emoji_id), None)
        if emoji is None:
            return None
        return CustomEmoji(
            native_id=str(emoji.id),
            name=emoji.name,
            image_url=emoji.image.url(),
            animated=getattr(emoji, "animated", False),
        )
