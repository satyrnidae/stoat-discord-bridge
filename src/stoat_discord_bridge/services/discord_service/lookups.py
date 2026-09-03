"""Platform-resource lookups for the Discord connector.

The `ConnectorInfo`-hook half of `DiscordSenderService`: resolving ids to
names and vice versa, get-or-create for roles / categories, and the
Category-membership helpers `/mirror channel` and `/link category` need.
All keyed off `self._client` / `self._config.guild_id`, which the composed
service provides. Composed into `DiscordSenderService`.
"""

from __future__ import annotations

import logging

import discord

from stoat_discord_bridge.models import ChannelMetadata, CustomEmoji
from stoat_discord_bridge.services.discord_service.formatting import _normalize_channel_id

# Discord text-channel topics are capped at 1024 chars; a longer source
# description is clipped rather than rejected.
_TOPIC_LIMIT = 1024

logger = logging.getLogger(__name__)


class DiscordLookupsMixin:
    """Resource-lookup half of `DiscordSenderService`."""

    def _guild_or_none(self) -> "discord.Guild | None":
        return self._client.get_guild(self._config.guild_id)

    async def get_channel_name(self, channel_id: str) -> str | None:
        """Best-effort channel-id -> name lookup, used as this connector's
        `ConnectorInfo.resolve_channel_name` for `/link channel`."""
        try:
            channel = self._client.get_channel(int(channel_id)) or await self._client.fetch_channel(int(channel_id))
        except (discord.HTTPException, discord.NotFound, ValueError):
            logger.debug("[discord:%s] couldn't resolve channel name for %s", self.connector_id, channel_id)
            return None
        return getattr(channel, "name", None)

    async def can_view_channel(self, channel_id: str) -> bool | None:
        """`ConnectorInfo.can_view_channel`: True if the bridge bot can see
        `channel_id` in this guild, False if the channel resolves but the bot
        lacks `view_channel` on it, None if it can't tell (bad id, uncached,
        error, or no bot member). `/mirror channel` refuses on an explicit
        False - Discord otherwise hands a bot without `view_channel` a
        placeholder name (`__hidden__`) that would become the mirrored
        channel's name (issue #33)."""
        guild = self._guild_or_none()
        if guild is None:
            return None
        try:
            cid = int(channel_id)
        except (TypeError, ValueError):
            return None
        channel = guild.get_channel_or_thread(cid)
        me = guild.me
        if channel is None or me is None:
            return None
        try:
            return bool(channel.permissions_for(me).view_channel)
        except Exception:
            return None

    async def resolve_channel_id_by_name(self, token: str) -> str | None:
        """Resolve a bare channel name to its id so the `/link channel` etc.
        commands accept either - this connector's
        `ConnectorInfo.resolve_channel_id_by_name`. A token that's already a
        real channel id (bare, or a pasted `<#id>` mention) is returned as
        the bare id; an unrecognized token yields None (ChannelLinker then
        treats it as a literal id). Case-insensitive; first match wins
        (Discord channel names aren't unique)."""
        token = _normalize_channel_id(token).strip()
        guild = self._guild_or_none()
        if guild is None:
            return None
        if token.isdigit() and guild.get_channel(int(token)) is not None:
            return token
        lowered = token.lstrip("#").casefold()
        for channel in (*guild.text_channels, *guild.voice_channels):
            if channel.name.casefold() == lowered:
                return str(channel.id)
        return None

    async def get_channel_category(self, channel_id: str) -> tuple[str, str] | None:
        """Best-effort channel-id -> (Category-id, Category-name), or None if
        the channel is uncategorised / unresolvable. This connector's
        `ConnectorInfo.resolve_channel_category`, used by `/mirror channel
        from` to land the new local channel in the linked local Category.
        Catches broadly (not just discord.py's own HTTPException/NotFound)
        since an id that isn't a real channel this client can see - e.g. a
        bare Stoat/IRC-style name typed into an id option - can fail in ways
        short of a clean discord.py exception (fetch_channel() isn't
        otherwise guarded here)."""
        try:
            channel = self._client.get_channel(int(channel_id)) or await self._client.fetch_channel(int(channel_id))
            category = getattr(channel, "category", None)
        except Exception:
            logger.debug("[discord:%s] couldn't resolve channel category for %s", self.connector_id, channel_id)
            return None
        if category is None:
            return None
        return str(category.id), category.name

    async def get_thread_parent(self, channel_id: str) -> tuple[str, str] | None:
        """If `channel_id` is a Discord thread (or forum post - also a
        `discord.Thread`), return its parent channel's `(id, name)`;
        otherwise None. This connector's `ConnectorInfo.resolve_thread_parent`
        - `ChannelLinker.mirror_channel` uses it so a manual `/mirror channel`
        on a thread groups the counterpart under a Category named after the
        thread's parent channel, like the automatic thread-create mirror
        (issue #72)."""
        try:
            channel = self._client.get_channel(int(channel_id)) or await self._client.fetch_channel(int(channel_id))
        except Exception:
            logger.debug("[discord:%s] couldn't resolve thread parent for %s", self.connector_id, channel_id)
            return None
        if not isinstance(channel, discord.Thread):
            return None
        parent = channel.parent
        if parent is None and channel.parent_id is not None:
            try:
                parent = self._client.get_channel(channel.parent_id) or await self._client.fetch_channel(
                    channel.parent_id
                )
            except Exception:
                parent = None
        if parent is None:
            return None
        return str(parent.id), getattr(parent, "name", str(parent.id))

    async def get_channel_category_name(self, channel_id: str) -> str | None:
        """Best-effort channel-id -> Category-name lookup, used by
        `/mirror channel` to carry a channel's Category across to the
        destination connector."""
        resolved = await self.get_channel_category(channel_id)
        return resolved[1] if resolved is not None else None

    async def get_category_name(self, category_id: str) -> str | None:
        """Best-effort Category-id -> name lookup, used as this connector's
        `ConnectorInfo.resolve_category_name` for `/link-category`'s
        destination-name resolution. Discord Categories are themselves
        channels (`discord.CategoryChannel`), so the same get_channel-or-
        fetch_channel pattern as get_channel_name resolves them directly."""
        try:
            category = self._client.get_channel(int(category_id)) or await self._client.fetch_channel(int(category_id))
        except Exception:
            logger.debug("[discord:%s] couldn't resolve category name for %s", self.connector_id, category_id)
            return None
        return getattr(category, "name", None)

    async def get_user_name(self, user_id: str) -> str | None:
        """Best-effort user-id -> display-name lookup, used as this
        connector's `ConnectorInfo.resolve_user_name` for `/linked-users`."""
        try:
            user = self._client.get_user(int(user_id)) or await self._client.fetch_user(int(user_id))
        except (discord.HTTPException, discord.NotFound, ValueError):
            logger.debug("[discord:%s] couldn't resolve user name for %s", self.connector_id, user_id)
            return None
        return getattr(user, "display_name", None)

    async def get_role_name(self, role_id: str) -> str | None:
        """Best-effort role-id -> name lookup, this connector's
        `ConnectorInfo.resolve_role_name`."""
        guild = self._guild_or_none()
        if guild is None:
            return None
        try:
            role = guild.get_role(int(role_id))
        except ValueError:
            return None
        return role.name if role is not None else None

    async def resolve_role_id_by_name(self, token: str) -> str | None:
        """Resolve a bare role name to its id so `/link-role` etc. accept
        either. A token that's already a real role id is returned as-is; an
        unrecognized token yields None (RoleLinker then treats it as a
        literal id). Case-insensitive; first match wins (Discord role names
        aren't unique)."""
        guild = self._guild_or_none()
        if guild is None:
            return None
        if token.isdigit() and guild.get_role(int(token)) is not None:
            return token
        lowered = token.casefold()
        for role in guild.roles:
            if role.name.casefold() == lowered:
                return str(role.id)
        return None

    async def resolve_user_id_by_name(self, token: str) -> str | None:
        """Resolve a bare display name / global name / username to a member
        id so `/link user` etc. accept either. A token that's already a real
        member id is returned as-is; an unrecognized token yields None
        (UserLinker then treats it as a literal id). Case-insensitive; first
        match wins. Needs the privileged members intent (enabled - see
        CLAUDE.md) for guild.members to be populated."""
        guild = self._guild_or_none()
        if guild is None:
            return None
        if token.isdigit() and guild.get_member(int(token)) is not None:
            return token
        lowered = token.casefold()
        for member in guild.members:
            names = (member.display_name, getattr(member, "global_name", None), member.name)
            if any(n is not None and n.casefold() == lowered for n in names):
                return str(member.id)
        return None

    async def ensure_role(self, name: str) -> str:
        """Get-or-create a role named `name`, returning its id - this
        connector's `ConnectorInfo.ensure_role` for `/mirror role`."""
        guild = self._guild_or_none()
        if guild is None:
            raise RuntimeError("Discord guild isn't cached yet - the bridge may still be connecting")
        lowered = name.casefold()
        for role in guild.roles:
            if role.name.casefold() == lowered:
                return str(role.id)
        role = await guild.create_role(name=name, reason="bridge role mirror")
        return str(role.id)

    async def get_emoji_name(self, emoji_id: str) -> str | None:
        """Best-effort emoji-id -> name lookup, this connector's
        `ConnectorInfo.resolve_emoji_name`."""
        guild = self._guild_or_none()
        if guild is None:
            return None
        try:
            emoji = guild.get_emoji(int(emoji_id))
        except ValueError:
            return None
        return emoji.name if emoji is not None else None

    async def resolve_emoji_id_by_name(self, token: str) -> str | None:
        """Resolve a bare custom-emoji name to its id so `/link emote` etc.
        accept either. A token that's already a real emoji id is returned
        as-is; an unrecognized token yields None (EmoteLinker then treats it
        as a literal id). Case-insensitive; first match wins."""
        guild = self._guild_or_none()
        if guild is None:
            return None
        if token.isdigit() and guild.get_emoji(int(token)) is not None:
            return token
        lowered = token.casefold()
        for emoji in guild.emojis:
            if emoji.name.casefold() == lowered:
                return str(emoji.id)
        return None

    async def resolve_emoji(self, emoji_id: str) -> "CustomEmoji | None":
        """emoji-id -> full CustomEmoji, this connector's
        `ConnectorInfo.resolve_emoji` (the source side of `/mirror emote`)."""
        guild = self._guild_or_none()
        if guild is None:
            return None
        try:
            emoji = guild.get_emoji(int(emoji_id))
        except ValueError:
            return None
        if emoji is None:
            return None
        return CustomEmoji(
            native_id=str(emoji.id), name=emoji.name, image_url=str(emoji.url), animated=emoji.animated
        )

    async def resolve_category_id_by_name(self, token: str) -> str | None:
        """Resolve a bare Category name to its id so `/link category` etc.
        accept either. A token that's already a real Category id is returned
        as-is; an unrecognized token yields None (CategoryLinker then treats
        it as a literal id). Case-insensitive; first match wins."""
        guild = self._guild_or_none()
        if guild is None:
            return None
        if token.isdigit():
            for category in guild.categories:
                if category.id == int(token):
                    return token
        lowered = token.casefold()
        for category in guild.categories:
            if category.name.casefold() == lowered:
                return str(category.id)
        return None

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

    # --- Autocomplete listing hooks (ConnectorInfo.list_*). Cache-only reads
    # off the cached guild - the `service`/`local_id` option autocomplete on
    # the /link etc. slash commands calls these on every keystroke, so they
    # must not do I/O. Each yields (id, name) pairs; an uncached guild yields
    # []. list_users needs the privileged members intent (enabled - see
    # CLAUDE.md) for guild.members to be populated. ---

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
