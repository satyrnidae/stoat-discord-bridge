"""Plain id <-> name resolution for the Discord connector: channels,
Categories, threads, users, and roles. All cache-only (no network) except
where a `fetch_*` fallback is noted. Keyed off `self._client` /
`self._config.guild_id`, which the composed `DiscordSenderService` provides.
"""

from __future__ import annotations

import logging

import discord

from stoat_discord_bridge.services.discord_service.formatting import _normalize_channel_id

logger = logging.getLogger(__name__)


class _NamesMixin:
    """Id <-> name resolution half of `DiscordLookupsMixin`."""

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
        the channel is uncategorized / unresolvable. This connector's
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
            logger.debug(
                "[discord:%s] couldn't resolve thread parent for %s", self.connector_id, channel_id, exc_info=True
            )
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
