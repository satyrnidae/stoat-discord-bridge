"""Role and custom-emoji get-or-create / resolve for the Discord connector -
`ensure_role` and the emoji trio (`get_emoji_name` / `resolve_emoji_id_by_name`
/ `resolve_emoji`) behind `/mirror role` and `/mirror emote`. Plain role-name
resolution (`get_role_name` / `resolve_role_id_by_name`) lives alongside the
rest of the id<->name lookups in `names.py`.
"""

from __future__ import annotations

from stoat_discord_bridge.models import CustomEmoji, EmojiCapacity


class _RolesEmojiMixin:
    """Role/emoji get-or-create half of `DiscordLookupsMixin`."""

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

    async def emoji_capacity(self) -> "EmojiCapacity | None":
        """Remaining static/animated emoji-slot capacity, this connector's
        `ConnectorInfo.emoji_capacity` (issue #157). `Guild.emoji_limit` is a
        cache-only property (boost-tier based) and `Guild.emojis` the
        already-cached emoji list, so this is a local read, not an API call.
        None if the guild isn't cached yet."""
        guild = self._guild_or_none()
        if guild is None:
            return None
        static_count = sum(1 for emoji in guild.emojis if not emoji.animated)
        animated_count = sum(1 for emoji in guild.emojis if emoji.animated)
        return EmojiCapacity(
            free_static=guild.emoji_limit - static_count, free_animated=guild.emoji_limit - animated_count
        )
