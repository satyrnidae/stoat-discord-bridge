"""The discord.py client subclass for the Discord connector.

discord.py dispatches events by looking up `on_<event>` attributes on the
Client instance itself, so *something* has to subclass `discord.Client`.
`_DiscordClient` exists only to satisfy that and delegates every callback to
the owning `DiscordSenderService`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import discord

if TYPE_CHECKING:
    from stoat_discord_bridge.services.discord_service.sender import DiscordSenderService


class _DiscordClient(discord.Client):
    """discord.py dispatches events by looking up `on_<event>` attributes on
    the Client instance itself, so *something* has to subclass discord.Client.
    This subclass exists only to satisfy that and delegates every callback to
    the owning DiscordSenderService, which otherwise doesn't need to inherit
    from a third-party client class."""

    def __init__(self, owner: "DiscordSenderService") -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        intents.emojis_and_stickers = True
        # Privileged - needed for on_member_update (role auto-grant, see
        # bridge.py's RoleSyncCoordinator). Must also be enabled in the
        # Discord developer portal or the gateway never sends the event.
        intents.members = True
        super().__init__(intents=intents)
        self._owner = owner

    async def on_ready(self) -> None:
        await self._owner._handle_ready()

    async def on_disconnect(self) -> None:
        await self._owner._handle_disconnect()

    async def on_message(self, message: discord.Message) -> None:
        await self._owner._handle_message(message)

    async def on_raw_message_edit(self, payload: discord.RawMessageUpdateEvent) -> None:
        await self._owner._handle_raw_message_edit(payload)

    async def on_thread_create(self, thread: discord.Thread) -> None:
        await self._owner._handle_thread_create(thread)

    async def on_typing(self, channel, user, when) -> None:
        await self._owner._handle_typing(channel, user)

    async def on_guild_channel_create(self, channel: discord.abc.GuildChannel) -> None:
        await self._owner._handle_channel_create(channel)

    async def on_member_update(self, before: discord.Member, after: discord.Member) -> None:
        await self._owner._handle_member_update(before, after)

    async def on_guild_role_update(self, before: discord.Role, after: discord.Role) -> None:
        await self._owner._handle_role_update(before, after)

    async def on_guild_role_delete(self, role: discord.Role) -> None:
        await self._owner._handle_role_delete(role)

    async def on_guild_channel_update(
        self, before: discord.abc.GuildChannel, after: discord.abc.GuildChannel
    ) -> None:
        await self._owner._handle_channel_update(before, after)

    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent) -> None:
        await self._owner._handle_raw_reaction(payload, added=True)

    async def on_raw_reaction_remove(self, payload: discord.RawReactionActionEvent) -> None:
        await self._owner._handle_raw_reaction(payload, added=False)

    async def on_guild_emojis_update(
        self, guild: discord.Guild, before: "list[discord.Emoji]", after: "list[discord.Emoji]"
    ) -> None:
        await self._owner._handle_guild_emojis_update(guild, before, after)
