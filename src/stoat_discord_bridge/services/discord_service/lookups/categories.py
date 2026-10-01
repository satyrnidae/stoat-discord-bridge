"""Category get-or-create / membership / move for the Discord connector -
`ensure_category`, `channels_in_category`, `move_channel_to_category`, the
`ConnectorInfo` hooks behind `/mirror category` - plus `threads_in_channel`,
the same kind of child listing for a text channel's threads. Discord's guild cache is
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

    async def ensure_category(self, name: str) -> tuple[str, bool]:
        """Get-or-create a Category named `name`, returning `(id, created)` -
        this connector's `ConnectorInfo.ensure_category` for `/mirror category`."""
        guild = self._guild_or_none()
        if guild is None:
            raise RuntimeError("Discord guild isn't cached yet - the bridge may still be connecting")
        lowered = name.casefold()
        for category in guild.categories:
            if category.name.casefold() == lowered:
                return str(category.id), False
        category = await guild.create_category(name, reason="bridge category mirror")
        return str(category.id), True

    async def channels_in_category(self, category_id: str) -> list[tuple[str, str]]:
        """Every channel inside Category `category_id`, as (id, name) pairs -
        this connector's `ConnectorInfo.channels_in_category`. A
        `discord.ForumChannel` is treated as a Category too (issue #100): its
        *active* threads (`forum.threads`) are the children. Archived posts
        are deliberately excluded - they're numerous and low-value, and any
        post still mirrors lazily via `_handle_thread_create` when it next
        sees activity. Anything that's neither a `CategoryChannel` nor a
        `ForumChannel` yields `[]`."""
        guild = self._guild_or_none()
        if guild is None:
            return []
        try:
            category = guild.get_channel(int(category_id))
        except ValueError:
            return []
        if isinstance(category, discord.ForumChannel):
            return [(str(t.id), t.name) for t in category.threads]
        if not isinstance(category, discord.CategoryChannel):
            return []
        return [(str(c.id), c.name) for c in category.channels]

    async def threads_in_channel(self, channel_id: str) -> list[tuple[str, str]]:
        """A text channel's *active* threads, as (id, name) pairs - this
        connector's `ConnectorInfo.threads_in_channel` (issue #225). Archived
        threads are left out, as in `channels_in_category`. Anything that
        isn't a `TextChannel` yields `[]`: voice channels can't have threads,
        and a forum's posts go through `channels_in_category` instead."""
        guild = self._guild_or_none()
        if guild is None:
            return []
        try:
            channel = guild.get_channel(int(channel_id))
        except ValueError:
            return []
        if not isinstance(channel, discord.TextChannel):
            return []
        return [(str(t.id), t.name) for t in channel.threads]

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
