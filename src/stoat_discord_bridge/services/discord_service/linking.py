"""The `/link`, `/unlink`, `/linked`, `/mirror` command handlers for Discord.

The Mongo-backed half: every method here is what `commands.build_command_tree`'s
callbacks forward to, and each one drives a shared linker (`ChannelLinker` /
`CategoryLinker` / `EmoteLinker` / `RoleLinker` / `UserLinker`) that reads
and writes the cross-connector mapping collections. The Manage-Server
execution gate is Discord-side, declared on the command groups themselves
(`default_permissions=manage_guild` in `commands.py`), so these don't
re-check it. Composed into `DiscordSenderService`.
"""

from __future__ import annotations

import logging

import discord

from stoat_discord_bridge.admin_commands import LinkError
from stoat_discord_bridge.services.discord_service.formatting import _normalize_channel_id, _normalize_role_id

logger = logging.getLogger(__name__)


class DiscordLinkingMixin:
    """Command-handler half of `DiscordSenderService`."""

    async def _handle_linked_channels(
        self, interaction: discord.Interaction, local_id: str | None = None
    ) -> None:
        if self._linker is None:
            await interaction.response.send_message("Linking isn't configured.", ephemeral=True)
            return
        channel_id = _normalize_channel_id(local_id) if local_id else str(interaction.channel_id)
        summary = await self._linker.list_linked_channels(
            local_connector=self.connector_id, local_channel_id=channel_id
        )
        await interaction.response.send_message(summary, ephemeral=True)

    async def _handle_linked_users(self, interaction: discord.Interaction, local_id: discord.Member | None) -> None:
        if self._user_linker is None:
            await interaction.response.send_message("User linking isn't configured.", ephemeral=True)
            return
        if local_id is not None:
            summary = await self._user_linker.list_linked_users(
                local_connector=self.connector_id, local_user_id=str(local_id.id)
            )
        else:
            summary = await self._user_linker.list_linked_users()
        await interaction.response.send_message(summary, ephemeral=True)

    async def _handle_link_channel(
        self, interaction: discord.Interaction, service: str, external_id: str, local_id: str | None
    ) -> None:
        if self._linker is None:
            await interaction.response.send_message("Linking isn't configured.", ephemeral=True)
            return
        external_id = _normalize_channel_id(external_id)
        if local_id is not None:
            local_id = _normalize_channel_id(local_id)
        logger.info(
            "[discord:%s] %s ran /link channel service=%s external_id=%s local_id=%s",
            self.connector_id,
            interaction.user.id,
            service,
            external_id,
            local_id,
        )
        try:
            summary = await self._linker.link_channel(
                local_connector=self.connector_id,
                local_channel_id=str(interaction.channel_id),
                local_channel_name=getattr(interaction.channel, "name", str(interaction.channel_id)),
                source=service,
                source_id=external_id,
                destination_id=local_id,
            )
        except LinkError as exc:
            logger.info("[discord:%s] /link channel rejected: %s", self.connector_id, exc)
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        await interaction.response.send_message(summary, ephemeral=True)

    def _invoking_category_id(self, interaction: discord.Interaction) -> str | None:
        category = getattr(interaction.channel, "category", None)
        return str(category.id) if category is not None else None

    async def _handle_linked_categories(
        self, interaction: discord.Interaction, local_id: str | None = None
    ) -> None:
        if self._category_linker is None:
            await interaction.response.send_message("Category linking isn't configured.", ephemeral=True)
            return
        local_id = _normalize_channel_id(local_id) if local_id else None
        if local_id is None and self._invoking_category_id(interaction) is None:
            await interaction.response.send_message("This channel isn't inside a Category.", ephemeral=True)
            return
        summary = await self._category_linker.list_linked_categories(
            local_connector=self.connector_id,
            local_category_id=self._invoking_category_id(interaction),
            local_category=local_id,
        )
        await interaction.response.send_message(summary, ephemeral=True)

    async def _handle_link_category(
        self, interaction: discord.Interaction, service: str, external_id: str, local_id: str | None
    ) -> None:
        if self._category_linker is None:
            await interaction.response.send_message("Category linking isn't configured.", ephemeral=True)
            return
        category = getattr(interaction.channel, "category", None)
        local_id = _normalize_channel_id(local_id) if local_id else None
        if category is None and local_id is None:
            await interaction.response.send_message("This channel isn't inside a Category.", ephemeral=True)
            return
        external_id = _normalize_channel_id(external_id)
        logger.info(
            "[discord:%s] %s ran /link category service=%s external_id=%s local_id=%s",
            self.connector_id,
            interaction.user.id,
            service,
            external_id,
            local_id,
        )
        try:
            summary = await self._category_linker.link_category(
                local_connector=self.connector_id,
                local_category_id=None if category is None else str(category.id),
                local_category_name="" if category is None else category.name,
                source=service,
                source_id=external_id,
                destination_id=local_id,
            )
        except LinkError as exc:
            logger.info("[discord:%s] /link category rejected: %s", self.connector_id, exc)
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        await interaction.response.send_message(summary, ephemeral=True)

    async def _handle_unlink_category(
        self, interaction: discord.Interaction, local_id: str | None = None, service: str | None = None
    ) -> None:
        if self._category_linker is None:
            await interaction.response.send_message("Category linking isn't configured.", ephemeral=True)
            return
        local_id = _normalize_channel_id(local_id) if local_id else None
        if local_id is None and self._invoking_category_id(interaction) is None:
            await interaction.response.send_message("This channel isn't inside a Category.", ephemeral=True)
            return
        logger.info(
            "[discord:%s] %s ran /unlink category local_id=%s service=%s",
            self.connector_id,
            interaction.user.id,
            local_id,
            service,
        )
        try:
            summary = await self._category_linker.unlink_category(
                local_connector=self.connector_id,
                local_category_id=self._invoking_category_id(interaction),
                local_category=local_id,
                destination=service,
            )
        except LinkError as exc:
            logger.info("[discord:%s] /unlink category rejected: %s", self.connector_id, exc)
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        await interaction.response.send_message(summary, ephemeral=True)

    async def _handle_mirror_category(
        self, interaction: discord.Interaction, local_id: str | None = None, service: str | None = None
    ) -> None:
        if self._category_linker is None:
            await interaction.response.send_message("Category linking isn't configured.", ephemeral=True)
            return
        local_id = _normalize_channel_id(local_id) if local_id else None
        if local_id is None and self._invoking_category_id(interaction) is None:
            await interaction.response.send_message("This channel isn't inside a Category.", ephemeral=True)
            return
        logger.info(
            "[discord:%s] %s ran /mirror category local_id=%s service=%s",
            self.connector_id,
            interaction.user.id,
            local_id,
            service,
        )
        await interaction.response.defer(ephemeral=True, thinking=True)
        kwargs = dict(
            local_connector=self.connector_id,
            local_category_id=self._invoking_category_id(interaction),
            local_category=local_id,
        )
        try:
            if service is None or service.lower() == "all":
                summary = await self._category_linker.mirror_category_all(**kwargs)
            else:
                summary = await self._category_linker.mirror_category(destination=service, **kwargs)
        except LinkError as exc:
            logger.info("[discord:%s] /mirror category rejected: %s", self.connector_id, exc)
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        await interaction.followup.send(summary or "Nothing to mirror.", ephemeral=True)

    async def _handle_mirror_category_from(
        self, interaction: discord.Interaction, service: str, external_id: str
    ) -> None:
        """`/mirror category from <service> <external_id>`: create a local
        Category mirroring `service`'s, link them, and relocate/mirror its
        channels into the local Category."""
        if self._category_linker is None:
            await interaction.response.send_message("Category linking isn't configured.", ephemeral=True)
            return
        logger.info(
            "[discord:%s] %s ran /mirror category from service=%s external_id=%s",
            self.connector_id,
            interaction.user.id,
            service,
            external_id,
        )
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            summary = await self._category_linker.mirror_category_from(
                local_connector=self.connector_id, source=service, source_id=external_id
            )
        except LinkError as exc:
            logger.info("[discord:%s] /mirror category from rejected: %s", self.connector_id, exc)
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        await interaction.followup.send(summary or "Nothing to mirror.", ephemeral=True)

    async def _handle_link_role(
        self, interaction: discord.Interaction, local_id: str, service: str, external_id: str
    ) -> None:
        if self._role_linker is None:
            await interaction.response.send_message("Role linking isn't configured.", ephemeral=True)
            return
        local_id = _normalize_role_id(local_id)
        external_id = _normalize_role_id(external_id)
        logger.info(
            "[discord:%s] %s ran /link-role local_id=%s service=%s external_id=%s",
            self.connector_id,
            interaction.user.id,
            local_id,
            service,
            external_id,
        )
        try:
            summary = await self._role_linker.link_role(
                local_connector=self.connector_id,
                local_role=local_id,
                source=service,
                source_role=external_id,
            )
        except LinkError as exc:
            logger.info("[discord:%s] /link-role rejected: %s", self.connector_id, exc)
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        await interaction.response.send_message(summary, ephemeral=True)

    async def _handle_unlink_role(
        self, interaction: discord.Interaction, local_id: str, service: str | None
    ) -> None:
        if self._role_linker is None:
            await interaction.response.send_message("Role linking isn't configured.", ephemeral=True)
            return
        local_id = _normalize_role_id(local_id)
        logger.info(
            "[discord:%s] %s ran /unlink-role local_id=%s service=%s",
            self.connector_id,
            interaction.user.id,
            local_id,
            service,
        )
        try:
            summary = await self._role_linker.unlink_role(
                local_connector=self.connector_id, local_role=local_id, destination=service
            )
        except LinkError as exc:
            logger.info("[discord:%s] /unlink-role rejected: %s", self.connector_id, exc)
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        await interaction.response.send_message(summary, ephemeral=True)

    async def _handle_linked_roles(
        self, interaction: discord.Interaction, local_id: str | None, service: str | None
    ) -> None:
        if self._role_linker is None:
            await interaction.response.send_message("Role linking isn't configured.", ephemeral=True)
            return
        summary = await self._role_linker.list_linked_roles(
            local_connector=self.connector_id,
            local_role=_normalize_role_id(local_id) if local_id else None,
            service=service,
        )
        await interaction.response.send_message(summary, ephemeral=True)

    async def _handle_mirror_role(
        self, interaction: discord.Interaction, local_id: str, service: str | None
    ) -> None:
        if self._role_linker is None:
            await interaction.response.send_message("Role linking isn't configured.", ephemeral=True)
            return
        local_id = _normalize_role_id(local_id)
        logger.info(
            "[discord:%s] %s ran /mirror-role local_id=%s service=%s",
            self.connector_id,
            interaction.user.id,
            local_id,
            service,
        )
        try:
            if service is None or service.lower() == "all":
                summary = await self._role_linker.mirror_role_all(
                    local_connector=self.connector_id, local_role=local_id
                )
            else:
                summary = await self._role_linker.mirror_role(
                    local_connector=self.connector_id, local_role=local_id, destination=service
                )
        except LinkError as exc:
            logger.info("[discord:%s] /mirror-role rejected: %s", self.connector_id, exc)
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        await interaction.response.send_message(summary, ephemeral=True)

    async def _handle_mirror_role_from(
        self, interaction: discord.Interaction, service: str, external_id: str
    ) -> None:
        """`/mirror role from <service> <external_id>`: create-or-match a
        local role mirroring `service`'s role, and link them."""
        if self._role_linker is None:
            await interaction.response.send_message("Role linking isn't configured.", ephemeral=True)
            return
        external_id = _normalize_role_id(external_id)
        logger.info(
            "[discord:%s] %s ran /mirror role from service=%s external_id=%s",
            self.connector_id,
            interaction.user.id,
            service,
            external_id,
        )
        try:
            summary = await self._role_linker.mirror_role_from(
                local_connector=self.connector_id, source=service, source_role=external_id
            )
        except LinkError as exc:
            logger.info("[discord:%s] /mirror role from rejected: %s", self.connector_id, exc)
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        await interaction.response.send_message(summary, ephemeral=True)

    async def _handle_link_emote(
        self, interaction: discord.Interaction, service: str, external_id: str, local_id: str
    ) -> None:
        if self._emote_linker is None:
            await interaction.response.send_message("Linking isn't configured.", ephemeral=True)
            return
        logger.info(
            "[discord:%s] %s ran /link emote service=%s external_id=%s local_id=%s",
            self.connector_id,
            interaction.user.id,
            service,
            external_id,
            local_id,
        )
        try:
            summary = await self._emote_linker.link_emote(
                local_connector=self.connector_id,
                local_id=local_id,
                source=service,
                source_id=external_id,
            )
        except LinkError as exc:
            logger.info("[discord:%s] /link emote rejected: %s", self.connector_id, exc)
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        await interaction.response.send_message(summary, ephemeral=True)

    async def _handle_unlink_emote(
        self, interaction: discord.Interaction, local_id: str, service: str | None
    ) -> None:
        if self._emote_linker is None:
            await interaction.response.send_message("Linking isn't configured.", ephemeral=True)
            return
        logger.info(
            "[discord:%s] %s ran /unlink emote local_id=%s service=%s",
            self.connector_id,
            interaction.user.id,
            local_id,
            service,
        )
        try:
            summary = await self._emote_linker.unlink_emote(
                local_connector=self.connector_id, local_emote=local_id, destination=service
            )
        except LinkError as exc:
            logger.info("[discord:%s] /unlink emote rejected: %s", self.connector_id, exc)
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        await interaction.response.send_message(summary, ephemeral=True)

    async def _handle_linked_emotes(
        self, interaction: discord.Interaction, local_id: str | None
    ) -> None:
        if self._emote_linker is None:
            await interaction.response.send_message("Linking isn't configured.", ephemeral=True)
            return
        summary = await self._emote_linker.list_linked_emotes(
            local_connector=self.connector_id, local_emote=local_id
        )
        await interaction.response.send_message(summary, ephemeral=True)

    async def _handle_mirror_emote(
        self, interaction: discord.Interaction, local_id: str, service: str | None
    ) -> None:
        if self._emote_linker is None:
            await interaction.response.send_message("Linking isn't configured.", ephemeral=True)
            return
        logger.info(
            "[discord:%s] %s ran /mirror emote local_id=%s service=%s",
            self.connector_id,
            interaction.user.id,
            local_id,
            service,
        )
        try:
            if service is None or service.lower() == "all":
                summary = await self._emote_linker.mirror_emote_all(
                    local_connector=self.connector_id, local_emote=local_id
                )
            else:
                summary = await self._emote_linker.mirror_emote(
                    local_connector=self.connector_id, local_emote=local_id, destination=service
                )
        except LinkError as exc:
            logger.info("[discord:%s] /mirror emote rejected: %s", self.connector_id, exc)
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        await interaction.response.send_message(summary, ephemeral=True)

    async def _handle_mirror_emote_from(
        self, interaction: discord.Interaction, service: str, external_id: str
    ) -> None:
        """`/mirror emote from <service> <external_id>`: recreate-or-match
        `service`'s custom emoji locally, and link them."""
        if self._emote_linker is None:
            await interaction.response.send_message("Linking isn't configured.", ephemeral=True)
            return
        logger.info(
            "[discord:%s] %s ran /mirror emote from service=%s external_id=%s",
            self.connector_id,
            interaction.user.id,
            service,
            external_id,
        )
        try:
            summary = await self._emote_linker.mirror_emote_from(
                local_connector=self.connector_id, source=service, source_emote=external_id
            )
        except LinkError as exc:
            logger.info("[discord:%s] /mirror emote from rejected: %s", self.connector_id, exc)
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        await interaction.response.send_message(summary, ephemeral=True)

    async def _handle_link_user(
        self, interaction: discord.Interaction, service: str, external_id: str, local_id: discord.Member
    ) -> None:
        # local_id is a real discord.Member (picked from Discord's own
        # member search, not typed as free text) specifically so this can't
        # end up linked to a mistyped/malformed id or a bare "@name" - see
        # LinkError-free "Unknown User"/`<@@name>` mangling that caused
        # further downstream once such a bad id was already on file.
        if self._user_linker is None:
            await interaction.response.send_message("User linking isn't configured.", ephemeral=True)
            return
        logger.info(
            "[discord:%s] %s ran /link user service=%s external_id=%s local_id=%s",
            self.connector_id,
            interaction.user.id,
            service,
            external_id,
            local_id.id,
        )
        try:
            summary = await self._user_linker.link_user(
                local_connector=self.connector_id,
                local_user_id=str(local_id.id),
                source=service,
                source_user_id=external_id,
            )
        except LinkError as exc:
            logger.info("[discord:%s] /link user rejected: %s", self.connector_id, exc)
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        await interaction.response.send_message(summary, ephemeral=True)

    async def _handle_mirror_channel(
        self, interaction: discord.Interaction, service: str | None, local_id: str | None
    ) -> None:
        if self._linker is None:
            await interaction.response.send_message("Linking isn't configured.", ephemeral=True)
            return
        if local_id is not None:
            channel_id = _normalize_channel_id(local_id)
            channel_name = await self.get_channel_name(channel_id) or channel_id
        else:
            channel_id = str(interaction.channel_id)
            channel_name = getattr(interaction.channel, "name", channel_id)
        channel_category = await self.get_channel_category_name(channel_id)
        logger.info(
            "[discord:%s] %s ran /mirror channel service=%s local_id=%s",
            self.connector_id,
            interaction.user.id,
            service,
            channel_id,
        )
        try:
            if service is None or service.lower() == "all":
                summary = await self._linker.mirror_channel_all(
                    local_connector=self.connector_id,
                    local_channel_id=channel_id,
                    local_channel_name=channel_name,
                    local_channel_category=channel_category,
                )
            else:
                summary = await self._linker.mirror_channel(
                    local_connector=self.connector_id,
                    local_channel_id=channel_id,
                    local_channel_name=channel_name,
                    destination=service,
                    local_channel_category=channel_category,
                )
        except LinkError as exc:
            logger.info("[discord:%s] /mirror channel rejected: %s", self.connector_id, exc)
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        await interaction.response.send_message(summary, ephemeral=True)

    async def _handle_mirror_channel_from(
        self, interaction: discord.Interaction, service: str, external_id: str
    ) -> None:
        """`/mirror channel from <service> <external_id>`: create a local
        channel mirroring `service`'s `external_id` and link them, placing it
        in the local counterpart of the source channel's linked Category."""
        if self._linker is None:
            await interaction.response.send_message("Linking isn't configured.", ephemeral=True)
            return
        external_id = _normalize_channel_id(external_id)
        logger.info(
            "[discord:%s] %s ran /mirror channel from service=%s external_id=%s",
            self.connector_id,
            interaction.user.id,
            service,
            external_id,
        )
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            summary = await self._linker.mirror_channel_from(
                local_connector=self.connector_id, source=service, source_id=external_id
            )
        except LinkError as exc:
            logger.info("[discord:%s] /mirror channel from rejected: %s", self.connector_id, exc)
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        await interaction.followup.send(summary or "Nothing to mirror.", ephemeral=True)

    async def _handle_unlink_channel(
        self, interaction: discord.Interaction, service: str | None, local_id: str | None
    ) -> None:
        if self._linker is None:
            await interaction.response.send_message("Linking isn't configured.", ephemeral=True)
            return
        channel_id = _normalize_channel_id(local_id) if local_id is not None else str(interaction.channel_id)
        logger.info(
            "[discord:%s] %s ran /unlink channel service=%s local_id=%s",
            self.connector_id,
            interaction.user.id,
            service,
            channel_id,
        )
        try:
            summary = await self._linker.unlink_channel(
                local_connector=self.connector_id, local_channel_id=channel_id, destination=service
            )
        except LinkError as exc:
            logger.info("[discord:%s] /unlink channel rejected: %s", self.connector_id, exc)
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        await interaction.response.send_message(summary, ephemeral=True)

    async def _handle_unlink_user(
        self, interaction: discord.Interaction, service: str | None, local_id: discord.Member | None
    ) -> None:
        if self._user_linker is None:
            await interaction.response.send_message("User linking isn't configured.", ephemeral=True)
            return
        target = local_id or interaction.user
        logger.info(
            "[discord:%s] %s ran /unlink user service=%s local_id=%s",
            self.connector_id,
            interaction.user.id,
            service,
            target.id,
        )
        try:
            summary = await self._user_linker.unlink_user(
                local_connector=self.connector_id, local_user_id=str(target.id), destination=service
            )
        except LinkError as exc:
            logger.info("[discord:%s] /unlink user rejected: %s", self.connector_id, exc)
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        await interaction.response.send_message(summary, ephemeral=True)
