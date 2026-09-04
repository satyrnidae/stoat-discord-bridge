"""Autocomplete listing hooks (`ConnectorInfo.list_*`) for the Stoat
connector. Cache-only reads off the cached server - Discord's `external_id`
option autocomplete on the /link etc. slash commands calls these on every
keystroke, so they stay on the same no-I/O `get_server(partial=True)` /
`_all_*` paths the bare-name resolvers use. `list_categories` is the
exception - it re-fetches (short-TTL-cached via `_fresh_categories`) because
stoat.py never refreshes the cached Category list from gateway events (issue
#66). Each yields (id, name) pairs; an uncached server yields [].
"""

from __future__ import annotations


class _ListingMixin:
    """Autocomplete-listing half of `StoatLookupsMixin`."""

    async def list_channels(self) -> list[tuple[str, str]]:
        try:
            server = self._client.get_server(self.server_id, partial=True)
            channels = list(getattr(server, "channels", []) or [])
        except Exception:
            return []
        return [(str(c.id), getattr(c, "name", "") or str(c.id)) for c in channels]

    async def list_categories(self) -> list[tuple[str, str]]:
        # Unlike the other list_* hooks this re-fetches (short-TTL-cached, see
        # `_fresh_categories`): stoat.py never refreshes the cached Category
        # list from gateway events, so autocomplete would otherwise never
        # show a Category created since startup (issue #66).
        try:
            categories = await self._fresh_categories()
            return [(str(c.id), getattr(c, "title", "") or str(c.id)) for c in categories]
        except Exception:
            return []

    async def list_roles(self) -> list[tuple[str, str]]:
        try:
            roles = self._all_roles()
        except Exception:
            return []
        return [(str(r.id), getattr(r, "name", "") or str(r.id)) for r in roles]

    async def list_users(self) -> list[tuple[str, str]]:
        try:
            members = self._all_members()
        except Exception:
            return []
        out: list[tuple[str, str]] = []
        for member in members:
            name = (
                getattr(member, "nick", None)
                or getattr(member, "display_name", None)
                or getattr(member, "name", None)
                or str(member.id)
            )
            out.append((str(member.id), name))
        return out

    async def list_emotes(self) -> list[tuple[str, str]]:
        try:
            emojis = await self._all_emojis()
        except Exception:
            return []
        return [(str(e.id), getattr(e, "name", "") or str(e.id)) for e in emojis]
