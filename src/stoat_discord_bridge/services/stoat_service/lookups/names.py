"""Plain id <-> name resolution for the Stoat connector: channels, the
server-level `get_channel`/`get_server` cache reads, `can_view_channel`, and
the role/member lookup helpers. All cache-only (no network) except where
noted. Keyed off `self.server_id` / `self._client`, which the composed
`StoatSenderService` provides.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


class _NamesMixin:
    """Id <-> name resolution half of `StoatLookupsMixin`."""

    def get_channel(self, channel_id: str, *, partial: bool = False):
        """Cache-only channel lookup (no network). Verified against stoat.py
        1.2.1 (`Client.get_channel`, `MapCache.get_channel`), not yet against
        a live server:

        - `partial=False` returns the fully-populated cached channel object
          (a `ServerChannel` - `.name` / `.category` / `.role_permissions` /
          `.category_id` all present) or `None` on a cache miss. It never
          raises for a missing channel and never does I/O.
        - `partial=True` returns that same cached channel, or a bare
          `PartialMessageable` stub (id + `Messageable` send/typing/
          fetch_message only - no `.name` etc.) on a miss, never `None`.

        So the `.name`/`.category`/`.role_permissions` readers below want
        `partial=False` and a `None` guard; the send/typing/fetch_message
        paths (receiver, reactions, pins) want `partial=True`.
        """
        return self._client.get_channel(channel_id, partial=partial)

    def get_server(self, server_id: str, *, partial: bool = False):
        return self._client.get_server(server_id, partial=partial)

    async def get_user_name(self, user_id: str) -> str | None:
        """Best-effort user-id -> display-name lookup, used as this
        connector's `ConnectorInfo.resolve_user_name` for `/linked-users`."""
        try:
            user = await self._client.fetch_user(user_id)
        except Exception:
            return None
        return getattr(user, "display_name", None) or getattr(user, "tag", None)

    async def can_view_channel(self, channel_id: str) -> bool | None:
        """`ConnectorInfo.can_view_channel`: True if the bridge bot can see
        `channel_id` on this server, False if the channel resolves but the
        bot's roles leave it without `view_channel` there, None if it can't
        tell (uncached channel, no self id yet, unresolvable bot member, or
        an error). `/mirror channel` refuses on an explicit False so a
        private channel the bot can't see is never mirrored (issue #33)."""
        if getattr(self, "_self_id", None) is None:
            return None
        channel = self._client.get_channel(channel_id, partial=False)
        if channel is None or not hasattr(channel, "permissions_for"):
            return None
        try:
            member = await self._client.get_server(self.server_id, partial=True).fetch_member(self._self_id)
        except Exception:
            logger.debug("[stoat:%s] couldn't fetch own member for channel-visibility check", self.connector_id)
            return None
        if member is None:
            return None
        try:
            return bool(channel.permissions_for(member).view_channel)
        except Exception:
            return None

    async def get_channel_name(self, channel_id: str) -> str | None:
        """Best-effort channel-id -> name lookup, used as this connector's
        `ConnectorInfo.resolve_channel_name` for `/link channel`.

        `get_channel(partial=False)` returns a fully-populated cached channel
        (`.name` present) or `None` on a cache miss - see `get_channel`'s
        docstring. The `getattr(..., None)` covers the miss; the `try` guards
        an unexpected raise only.
        """
        try:
            channel = self._client.get_channel(channel_id, partial=False)
        except Exception:
            return None
        return getattr(channel, "name", None)

    async def resolve_channel_id_by_name(self, token: str) -> str | None:
        """Resolve a bare channel name to its id so the `/link channel` etc.
        commands accept either - this connector's
        `ConnectorInfo.resolve_channel_id_by_name`. A token that's already a
        real channel id is returned as-is; an unrecognized token yields None
        (ChannelLinker then treats it as a literal id). Case-insensitive;
        first match wins."""
        try:
            server = self._client.get_server(self.server_id, partial=True)
            channels = list(getattr(server, "channels", []) or [])
        except Exception:
            return None
        if any(str(c.id) == token for c in channels):
            return token
        lowered = token.casefold()
        for channel in channels:
            if getattr(channel, "name", "").casefold() == lowered:
                return str(channel.id)
        return None

    async def get_channel_category(self, channel_id: str) -> tuple[str, str] | None:
        """Best-effort channel-id -> (Category-id, Category-title), or None if
        uncategorized / unresolvable. This connector's
        `ConnectorInfo.resolve_channel_category`, used by `/mirror channel
        from` to land the new local channel in the linked local Category.
        `get_channel(partial=False)` returns the cached channel or `None` on a
        miss (see `get_channel`'s docstring); `None.category` and a genuine
        cache-miss `NoData` from `.category` both land in the `except` and
        yield `None`, same best-effort pattern as get_channel_name elsewhere
        in this class."""
        try:
            channel = self._client.get_channel(channel_id, partial=False)
            category = channel.category
        except Exception:
            return None
        if category is None:
            return None
        return str(category.id), category.title

    async def get_channel_category_name(self, channel_id: str) -> str | None:
        """Best-effort channel-id -> Category-title lookup, for `/mirror
        channel` to carry a channel's Category across to the destination
        connector."""
        resolved = await self.get_channel_category(channel_id)
        return resolved[1] if resolved is not None else None

    async def get_category_name(self, category_id: str) -> str | None:
        """Best-effort Category-id -> title lookup, used as this connector's
        `ConnectorInfo.resolve_category_name` for `/link-category`. Unlike
        get_channel_name/get_channel_category_name, there's no direct
        "get Category by id" call on stoat.Server, so this scans the Category
        list for a matching id instead - the freshly-fetched one, since the
        cache doesn't track Categories added since startup (see
        `_fresh_categories`, issue #66)."""
        try:
            categories = await self._fresh_categories()
            category = next((c for c in categories if str(c.id) == category_id), None)
            return category.title if category is not None else None
        except Exception:
            return None

    async def get_role_name(self, role_id: str) -> str | None:
        """Best-effort role-id -> name lookup, this connector's
        `ConnectorInfo.resolve_role_name`."""
        try:
            role = self._role_by_id(role_id)
        except Exception:
            return None
        return getattr(role, "name", None) if role is not None else None

    async def resolve_role_id_by_name(self, token: str) -> str | None:
        """Resolve a bare role name to its id (case-insensitive, first match);
        a token that's already a role id is returned as-is, an unknown token
        yields None."""
        try:
            roles = self._all_roles()
        except Exception:
            return None
        for role in roles:
            if str(getattr(role, "id", "")) == token:
                return token
        lowered = token.casefold()
        for role in roles:
            if str(getattr(role, "name", "")).casefold() == lowered:
                return str(role.id)
        return None

    @staticmethod
    def _roles_of(server):
        roles = getattr(server, "roles", None) or []
        return list(roles.values()) if isinstance(roles, dict) else list(roles)

    def _all_roles(self):
        server = self._client.get_server(self.server_id, partial=True)
        return self._roles_of(server)

    def _role_by_id(self, role_id: str):
        return next((r for r in self._all_roles() if str(getattr(r, "id", "")) == role_id), None)

    @staticmethod
    def _members_of(server):
        # `BaseServer.members` is a `Mapping[str, Member]` keyed by user id - a
        # plain `dict` off the cache, or `{}` when the server isn't cached
        # (`get_server(partial=True)` then hands back a bare `BaseServer`, which
        # still carries the property). Verified against stoat.py 1.2.1, not yet
        # against a live server. The `list(members)` branch is a defensive
        # fallback, mirroring `_roles_of`.
        members = getattr(server, "members", None) or []
        return list(members.values()) if isinstance(members, dict) else list(members)

    def _all_members(self):
        server = self._client.get_server(self.server_id, partial=True)
        return self._members_of(server)

    async def resolve_user_id_by_name(self, token: str) -> str | None:
        """Resolve a bare display name / nickname / username to a member id
        (case-insensitive, first match) so `/link user` etc. accept either; a
        token that's already a member id is returned as-is, an unknown token
        yields None (UserLinker then treats it as a literal id)."""
        try:
            members = self._all_members()
        except Exception:
            return None
        for member in members:
            if str(getattr(member, "id", "")) == token:
                return token
        lowered = token.casefold()
        for member in members:
            candidates = (
                getattr(member, "nick", None),
                getattr(member, "display_name", None),
                getattr(member, "name", None),
            )
            if any(c and c.casefold() == lowered for c in candidates):
                return str(member.id)
        return None
