"""Category get-or-create / membership / move for the Discord connector -
`ensure_category`, `channels_in_category`, `move_channel_to_category`, the
`ConnectorInfo` hooks behind `/mirror category`. Discord's guild cache is
kept live by gateway events (unlike Stoat's - see CLAUDE.md), so unlike
`stoat_service/lookups/categories.py` there's no separate freshness/refresh
concern to split out here.
"""

from __future__ import annotations

import logging

import discord

logger = logging.getLogger(__name__)


class _CategoriesMixin:
    """Category get-or-create/membership half of `DiscordLookupsMixin`."""

    async def ensure_category(self, name: str) -> str:
        """Get-or-create a Category named `name`, returning its id - this
        connector's `ConnectorInfo.ensure_category` for `/mirror category`."""
        guild = self._guild_or_none()
        if guild is None:
            raise RuntimeError("Discord guild isn't cached yet - the bridge may still be connecting")
        lowered = name.casefold()
        for category in guild.categories:
            if category.name.casefold() == lowered:
                return str(category.id)
        category = await guild.create_category(name, reason="bridge category mirror")
        return str(category.id)

    async def channels_in_category(self, category_id: str) -> list[tuple[str, str]]:
        """Every channel inside Category `category_id`, as (id, name) pairs -
        this connector's `ConnectorInfo.channels_in_category`."""
        guild = self._guild_or_none()
        if guild is None:
            return []
        try:
            category = guild.get_channel(int(category_id))
        except ValueError:
            return []
        if not isinstance(category, discord.CategoryChannel):
            return []
        return [(str(c.id), c.name) for c in category.channels]

    async def move_channel_to_category(self, channel_id: str, category_id: str) -> None:
        """Move channel `channel_id` into Category `category_id` - idempotent,
        best-effort (logs and swallows failures)."""
        guild = self._guild_or_none()
        if guild is None:
            return
        try:
            channel = guild.get_channel(int(channel_id))
            category = guild.get_channel(int(category_id))
        except ValueError:
            return
        if channel is None or not isinstance(category, discord.CategoryChannel):
            return
        if getattr(channel, "category_id", None) == category.id:
            return
        try:
            await channel.edit(category=category, reason="bridge category mirror")
        except Exception:
            logger.exception(
                "[discord:%s] category mirror: move of channel %s into %s failed",
                self.connector_id,
                channel_id,
                category_id,
            )
