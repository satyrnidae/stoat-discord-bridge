"""Channel get-or-create for the Discord connector - `ensure_channel` /
`describe_channel`, the `ConnectorInfo.ensure_channel` /
`ConnectorInfo.describe_channel` hooks behind `/mirror channel` and Discord
thread mirroring. Category placement for the channel this creates is done
inline (Discord Categories are matched/created by name directly, unlike
Stoat's raw-HTTP fallback), since there's no separate freshness/cache-refresh
concern here - Discord's guild cache is kept live by gateway events.
"""

from __future__ import annotations

import logging

import discord

from stoat_discord_bridge.models import ChannelMetadata

logger = logging.getLogger(__name__)

# Discord text-channel topics are capped at 1024 chars; a longer source
# description is clipped rather than rejected.
_TOPIC_LIMIT = 1024


class _ChannelsMixin:
    """Channel get-or-create half of `DiscordLookupsMixin`."""

    async def describe_channel(self, channel_id: str) -> ChannelMetadata | None:
        """Best-effort read of a channel's topic (as `description`) and NSFW
        flag, this connector's `ConnectorInfo.describe_channel` - `/mirror
        channel` carries it onto the mirrored copy (issue #32). Discord guild
        text channels have no per-channel icon, so `icon_url` is always None
        here. Returns None if the channel isn't resolvable."""
        try:
            channel = self._client.get_channel(int(channel_id)) or await self._client.fetch_channel(int(channel_id))
        except Exception:
            logger.debug("[discord:%s] couldn't resolve channel %s for describe", self.connector_id, channel_id)
            return None
        if channel is None:
            return None
        return ChannelMetadata(
            description=getattr(channel, "topic", None),
            nsfw=bool(getattr(channel, "nsfw", False)),
            icon_url=None,
        )

    async def ensure_channel(
        self,
        name: str,
        category: str | None = None,
        is_thread_category: bool = False,
        category_parent_channel_id: str | None = None,
        *,
        metadata: ChannelMetadata | None = None,
    ) -> str:
        """Idempotent get-or-create text channel by name, this connector's
        `ConnectorInfo.ensure_channel` for `/mirror channel`. Matches an
        existing text channel by name (case-insensitive), else creates one;
        if `category` is given, the channel ends up under a same-named
        Category (created if needed). `is_thread_category` /
        `category_parent_channel_id` bind that Category as thread-only
        (`CategoryLinker.bind_thread_category`), same as the Stoat hook, so
        `/link category` later refuses it. `metadata`, when given, sets the
        new channel's topic / NSFW flag - *only when this call creates the
        channel* (issue #32); a matched channel is left as-is."""
        guild = self._guild_or_none()
        if guild is None:
            raise RuntimeError("Discord guild isn't cached yet - the bridge may still be connecting")

        lowered = name.casefold()
        channel = next((c for c in guild.text_channels if c.name.casefold() == lowered), None)

        parent: discord.CategoryChannel | None = None
        if category is not None:
            parent = next((c for c in guild.categories if c.name.casefold() == category.casefold()), None)
            if parent is None:
                parent = await guild.create_category(category, reason="bridge channel mirror")

        if channel is None:
            create_kwargs: dict = {}
            if parent is not None:
                create_kwargs["category"] = parent
            if metadata is not None:
                if metadata.description:
                    create_kwargs["topic"] = metadata.description[:_TOPIC_LIMIT]
                if metadata.nsfw:
                    create_kwargs["nsfw"] = True
            channel = await guild.create_text_channel(name, reason="bridge channel mirror", **create_kwargs)
        elif parent is not None and getattr(channel, "category_id", None) != parent.id:
            try:
                await channel.edit(category=parent, reason="bridge channel mirror")
            except Exception:
                logger.exception(
                    "[discord:%s] channel mirror: move of %s into category %s failed",
                    self.connector_id,
                    channel.id,
                    parent.id,
                )

        if (
            is_thread_category
            and parent is not None
            and category_parent_channel_id is not None
            and self._category_linker is not None
        ):
            try:
                await self._category_linker.bind_thread_category(
                    self.connector_id, category_parent_channel_id, str(parent.id)
                )
            except Exception:
                logger.exception(
                    "[discord:%s] failed to bind category %s to parent %s",
                    self.connector_id,
                    parent.id,
                    category_parent_channel_id,
                )

        return str(channel.id)
