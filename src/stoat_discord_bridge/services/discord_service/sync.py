"""Reaction / emoji / role / channel-permission sync handlers for Discord.

The `_DiscordClient` gateway callbacks for member/role/channel/emoji/reaction
events land here, turning a native discord.py event into the bridge's
`Standard*` shape, plus the receiver-facing hooks the `RoleSyncCoordinator`
calls back in (`grant_role`, `rename_role`, `set_channel_role_permission`,
…). Composed into `DiscordSenderService`.
"""

from __future__ import annotations

import logging

import discord

from stoat_discord_bridge.models import (
    CustomEmoji,
    StandardEmojiCreated,
    StandardEmojiDeleted,
)
from stoat_discord_bridge.services.discord_service.formatting import (
    _MAPPED_DISCORD_PERM_ATTRS,
    _discord_reaction_matches,
    _to_standard_reaction,
)
from stoat_discord_bridge.services.role_sync import (
    discord_overwrite_to_neutral,
    neutral_to_discord_pair,
)

logger = logging.getLogger(__name__)


class DiscordSyncMixin:
    """Sync-event half of `DiscordSenderService`."""

    async def _handle_member_update(self, before: discord.Member, after: discord.Member) -> None:
        """A guild member changed - if their role set changed and a callback
        is wired, report (added, removed) role ids for role auto-grant. Needs
        the privileged members intent (see _DiscordClient.__init__)."""
        if self._on_member_roles_changed is None or after.guild.id != self._config.guild_id:
            return
        before_ids = {str(r.id) for r in before.roles}
        after_ids = {str(r.id) for r in after.roles}
        added = after_ids - before_ids
        removed = before_ids - after_ids
        if not added and not removed:
            return
        await self._on_member_roles_changed(self.connector_id, str(after.id), added, removed)

    async def _handle_role_update(self, before: discord.Role, after: discord.Role) -> None:
        """A guild role changed - propagate a rename to linked copies."""
        if self._on_role_renamed is None or after.guild.id != self._config.guild_id:
            return
        if before.name == after.name:
            return
        await self._on_role_renamed(self.connector_id, str(after.id), after.name)

    async def _handle_role_delete(self, role: discord.Role) -> None:
        if self._on_role_deleted is None or role.guild.id != self._config.guild_id:
            return
        await self._on_role_deleted(self.connector_id, str(role.id))

    async def _handle_channel_update(
        self, before: discord.abc.GuildChannel, after: discord.abc.GuildChannel
    ) -> None:
        """A channel (or category) was edited - if a role's permission
        overwrite changed and the callback is wired, report the new
        override for permission mirroring. This event fires for many
        unrelated edits, so it diffs the overwrites and no-ops fast."""
        if self._on_channel_role_permission_changed is None or getattr(after, "guild", None) is None:
            return
        if after.guild.id != self._config.guild_id:
            return
        before_ov = {t: o for t, o in before.overwrites.items() if isinstance(t, discord.Role)}
        after_ov = {t: o for t, o in after.overwrites.items() if isinstance(t, discord.Role)}
        changed = set(before_ov) | set(after_ov)
        is_category = isinstance(after, discord.CategoryChannel)
        for role in changed:
            b = before_ov.get(role)
            a = after_ov.get(role)
            if b == a:
                continue
            allow, deny = (a.pair() if a is not None else discord.PermissionOverwrite().pair())
            override = discord_overwrite_to_neutral(allow, deny)
            await self._on_channel_role_permission_changed(
                self.connector_id, str(after.id), str(role.id), override, is_category=is_category
            )

    async def get_channel_role_permission(self, channel_id: str, role_id: str):
        guild = self._guild_or_none()
        if guild is None:
            return None
        try:
            channel = guild.get_channel(int(channel_id))
            role = guild.get_role(int(role_id))
        except ValueError:
            return None
        if channel is None or role is None:
            return None
        allow, deny = channel.overwrites_for(role).pair()
        return discord_overwrite_to_neutral(allow, deny)

    async def set_channel_role_permission(self, channel_id: str, role_id: str, override) -> None:
        """Idempotent - skips the API call if the overwrite already matches."""
        guild = self._guild_or_none()
        if guild is None:
            return
        try:
            channel = guild.get_channel(int(channel_id))
            role = guild.get_role(int(role_id))
        except ValueError:
            return
        if channel is None or role is None:
            return
        current = channel.overwrites_for(role)
        cur_allow, cur_deny = current.pair()
        if discord_overwrite_to_neutral(cur_allow, cur_deny) == override:
            return
        allow, deny = neutral_to_discord_pair(override, discord.Permissions)
        new = discord.PermissionOverwrite.from_pair(allow, deny)
        # keep every unmapped bit exactly as the current overwrite had it -
        # mirroring only ever touches the shared NEUTRAL_PERMISSIONS subset.
        for name, value in current:
            if name not in _MAPPED_DISCORD_PERM_ATTRS:
                setattr(new, name, value)
        try:
            await channel.set_permissions(role, overwrite=new, reason="bridge role permission sync")
        except Exception:
            logger.exception("[discord:%s] perm sync: set on channel %s role %s failed", self.connector_id, channel_id, role_id)

    async def rename_role(self, role_id: str, new_name: str) -> None:
        """Idempotent - skips the API call if the role already has that name,
        so the rename echo doesn't loop. Best-effort."""
        guild = self._guild_or_none()
        if guild is None:
            return
        try:
            role = guild.get_role(int(role_id))
        except ValueError:
            return
        if role is None or role.name == new_name:
            return
        try:
            await role.edit(name=new_name, reason="bridge role sync")
        except Exception:
            logger.exception("[discord:%s] role sync: rename of %s failed", self.connector_id, role_id)

    async def grant_role(self, user_id: str, role_id: str) -> None:
        """Idempotent - no-op (no API call) if the member already has the
        role, so the role-grant echo doesn't loop. Best-effort; logs and
        swallows failures (missing member/role, hierarchy, permissions)."""
        await self._edit_member_role(user_id, role_id, add=True)

    async def revoke_role(self, user_id: str, role_id: str) -> None:
        await self._edit_member_role(user_id, role_id, add=False)

    async def _edit_member_role(self, user_id: str, role_id: str, *, add: bool) -> None:
        guild = self._guild_or_none()
        if guild is None:
            return
        try:
            member = guild.get_member(int(user_id)) or await guild.fetch_member(int(user_id))
            role = guild.get_role(int(role_id))
        except Exception:
            logger.warning("[discord:%s] role sync: couldn't resolve member %s / role %s", self.connector_id, user_id, role_id)
            return
        if role is None or member is None:
            return
        has = role in member.roles
        if has == add:
            return
        try:
            if add:
                await member.add_roles(role, reason="bridge role sync")
            else:
                await member.remove_roles(role, reason="bridge role sync")
        except Exception:
            logger.exception("[discord:%s] role sync: %s role %s for %s failed", self.connector_id, "add" if add else "remove", role_id, user_id)

    async def _handle_channel_create(self, channel: discord.abc.GuildChannel) -> None:
        """A new channel appeared in this guild - if it landed inside a
        Category that's linked via /link-category, auto-sync it onto the
        other connectors' own linked Categories (CategoryLinker.sync_new_channel).
        No-op for a channel outside any Category, or one whose Category was
        never linked - see that method's own no-op behavior."""
        if self._category_linker is None or channel.guild.id != self._config.guild_id:
            return
        if not isinstance(channel, (discord.TextChannel, discord.VoiceChannel)):
            return
        category = getattr(channel, "category", None)
        if category is None:
            return
        try:
            await self._category_linker.sync_new_channel(
                local_connector=self.connector_id,
                local_category_id=str(category.id),
                channel_id=str(channel.id),
                channel_name=channel.name,
            )
        except Exception:
            logger.exception("[discord:%s] failed to auto-sync new channel %s", self.connector_id, channel.id)

    async def _handle_raw_reaction(self, payload: discord.RawReactionActionEvent, *, added: bool) -> None:
        if self._on_reaction is None or payload.guild_id != self._config.guild_id:
            return
        if payload.user_id == getattr(self._client.user, "id", None):
            return  # the bridge's own mirrored reaction landing back here - drop it, don't re-relay
        if self._is_other_bot(payload):
            return
        count = await self._reactor_count(payload)
        await self._on_reaction(_to_standard_reaction(payload, self.connector_id, added=added, reactor_count=count))

    async def _reactor_count(self, payload: discord.RawReactionActionEvent) -> int | None:
        """How many users hold `payload.emoji` on the message after this
        event. `RawReactionActionEvent` doesn't carry it, so fetch the
        message; a fetch failure returns None and the coordinator acts
        best-effort."""
        try:
            channel = self._client.get_channel(payload.channel_id) or await self._client.fetch_channel(
                payload.channel_id
            )
            message = await channel.fetch_message(payload.message_id)
        except Exception:
            return None
        for reaction in message.reactions:
            if _discord_reaction_matches(reaction.emoji, payload.emoji):
                return reaction.count
        return 0

    def _is_other_bot(self, payload: discord.RawReactionActionEvent) -> bool:
        # `payload.member` is only ever populated for REACTION_ADD - discord.py
        # leaves it None for REACTION_REMOVE - so that check alone silently
        # never filters bot reaction removals. Fall back to the client's user
        # cache there; best-effort (a cache miss lets the removal through),
        # but still symmetric with the add path in the common case.
        if payload.member is not None:
            return payload.member.bot
        user = self._client.get_user(payload.user_id)
        return user is not None and user.bot

    async def _handle_guild_emojis_update(
        self, guild: discord.Guild, before: "list[discord.Emoji]", after: "list[discord.Emoji]"
    ) -> None:
        if guild.id != self._config.guild_id:
            return
        before_ids = {e.id for e in before}
        after_ids = {e.id for e in after}

        if self._on_emoji_created is not None:
            for emoji in after:
                if emoji.id in before_ids:
                    continue
                if emoji.user is not None and emoji.user.bot:
                    continue  # the bridge's own mirrored emoji landing back here - drop it, don't re-mirror
                await self._on_emoji_created(
                    StandardEmojiCreated(
                        origin_connector_id=self.connector_id,
                        emoji=CustomEmoji(
                            native_id=str(emoji.id), name=emoji.name, image_url=str(emoji.url), animated=emoji.animated
                        ),
                    )
                )

        if self._on_emoji_deleted is not None:
            for emoji in before:
                if emoji.id in after_ids:
                    continue
                await self._on_emoji_deleted(
                    StandardEmojiDeleted(origin_connector_id=self.connector_id, native_id=str(emoji.id))
                )
