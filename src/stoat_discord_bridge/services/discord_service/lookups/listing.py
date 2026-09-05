"""Autocomplete listing hooks (`ConnectorInfo.list_*`) for the Discord
connector. Cache-only reads off the cached guild - the `service`/`local_id`
option autocomplete on the /link etc. slash commands calls these on every
keystroke, so they must not do I/O. Each yields (id, name) pairs; an
uncached guild yields []. `list_users` needs the privileged members intent
(enabled - see CLAUDE.md) for `guild.members` to be populated.
"""

from __future__ import annotations


class _ListingMixin:
    """Autocomplete-listing half of `DiscordLookupsMixin`."""

    async def list_channels(self) -> list[tuple[str, str]]:
        guild = self._guild_or_none()
        if guild is None:
            return []
        return [(str(c.id), c.name) for c in (*guild.text_channels, *guild.voice_channels)]

    async def list_categories(self) -> list[tuple[str, str]]:
        guild = self._guild_or_none()
        if guild is None:
            return []
        return [(str(c.id), c.name) for c in guild.categories]

    async def list_roles(self) -> list[tuple[str, str]]:
        guild = self._guild_or_none()
        if guild is None:
            return []
        return [(str(r.id), r.name) for r in guild.roles if not r.is_default()]

    async def list_users(self) -> list[tuple[str, str]]:
        guild = self._guild_or_none()
        if guild is None:
            return []
        return [(str(m.id), m.display_name) for m in guild.members]

    async def list_emotes(self) -> list[tuple[str, str]]:
        guild = self._guild_or_none()
        if guild is None:
            return []
        return [(str(e.id), e.name) for e in guild.emojis]
