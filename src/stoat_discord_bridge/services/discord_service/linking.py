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

import contextlib
import logging
from collections.abc import Awaitable
from typing import Any

import discord

from stoat_discord_bridge.admin_commands import LinkError
from stoat_discord_bridge.services.discord_service.editor import LinkEditorSpec, LinkEditorView
from stoat_discord_bridge.services.discord_service.formatting import _normalize_channel_id, _normalize_role_id

logger = logging.getLogger(__name__)


class DiscordLinkingMixin:
    """Command-handler half of `DiscordSenderService`."""

    async def _linker_configured(
        self, interaction: discord.Interaction, linker: object | None, message: str
    ) -> bool:
        """True if `linker` is configured; otherwise replies `message`
        (ephemeral) and returns False, so a handler opens with
        `if not await self._linker_configured(interaction, self._x_linker,
        "...isn't configured."): return` instead of repeating the
        None-check/reply/return three-liner (issue #106)."""
        if linker is not None:
            return True
        await interaction.response.send_message(message, ephemeral=True)
        return False

    async def _send_linker_reply(
        self,
        interaction: discord.Interaction,
        content: str,
        *,
        deferred: bool = False,
        editor: LinkEditorSpec | None = None,
    ) -> None:
        """Send `content` (a linker summary or read-only listing), attaching
        a `LinkEditorView` in-line editor (issue #115) when `editor` is
        given. `deferred` picks `interaction.followup.send` over
        `interaction.response.send_message`, same as `_reply_linker_result`.

        `interaction.response.send_message` always returns None in the real
        API (the message has to be fetched back via
        `interaction.original_response()`); `interaction.followup.send`
        returns the sent message directly. Either way, the resulting message
        is stashed on the view so `on_timeout` (which has no interaction of
        its own to respond through) can still disable the panel in place."""
        view = await LinkEditorView.create(editor, invoker_id=interaction.user.id, content=content) if editor else None
        reply = interaction.followup.send if deferred else interaction.response.send_message
        kwargs: dict[str, Any] = {"ephemeral": True}
        if view is not None:
            kwargs["view"] = view
        sent = await reply(content, **kwargs)
        if view is not None:
            message = sent
            if message is None:
                with contextlib.suppress(discord.HTTPException, discord.NotFound):
                    message = await interaction.original_response()
            view.message = message

    async def _reply_linker_result(
        self,
        interaction: discord.Interaction,
        coro: Awaitable[str],
        *,
        log_context: str,
        deferred: bool = False,
        empty_fallback: str | None = None,
        editor: LinkEditorSpec | None = None,
    ) -> None:
        """The `try: summary = await <linker call> / except LinkError: log +
        reply(str(exc)) / else: reply(summary)` shape every mutating
        handler ends with. `deferred` picks `interaction.followup.send`
        (a handler that already called `interaction.response.defer()` for a
        slow mirror) over `interaction.response.send_message`;
        `empty_fallback` (the mirror handlers' "Nothing to mirror.") is
        substituted for a falsy `summary`, matching each handler's own
        `summary or "..."` it used to write inline (issue #106). `editor`,
        when given, attaches the in-line link editor (issue #115) to a
        successful, non-empty reply only - there's nothing to edit on an
        error or a "Nothing to mirror" no-op."""
        try:
            summary = await coro
        except LinkError as exc:
            logger.info("[discord:%s] %s rejected: %s", self.connector_id, log_context, exc)
            reply = interaction.followup.send if deferred else interaction.response.send_message
            await reply(str(exc), ephemeral=True)
            return
        content = summary if empty_fallback is None else (summary or empty_fallback)
        await self._send_linker_reply(interaction, content, deferred=deferred, editor=editor if summary else None)

    async def _listing_editor(
        self, interaction: discord.Interaction, kind: str, linker: Any, local_id: str
    ) -> LinkEditorSpec | None:
        """The `LinkEditorSpec` for a `/linked <noun>` listing's Edit
        controls (issue #115) - None if the invoker can't manage the guild,
        `local_id` isn't actually linked, or its group has no other member
        to edit. Anchors on the *first* non-local member; the panel's own
        connector select can retarget to any other member from there."""
        permissions = getattr(interaction.user, "guild_permissions", None)
        if permissions is not None and not permissions.manage_guild:
            return None
        described = await linker.describe_group(local_connector=self.connector_id, local_id=local_id)
        if described is None:
            return None
        _group_id, members = described
        local_member = next((m for m in members if m.connector_id == self.connector_id), None)
        edited_member = next((m for m in members if m.connector_id != self.connector_id), None)
        if local_member is None or edited_member is None:
            return None
        return LinkEditorSpec(
            kind=kind,
            linker=linker,
            local_connector=self.connector_id,
            local_id=local_member.entity_id,
            local_name=local_member.name,
            edited_connector=edited_member.connector_id,
        )

    async def _handle_linked_channels(
        self, interaction: discord.Interaction, local_id: str | None = None
    ) -> None:
        if not await self._linker_configured(interaction, self._linker, "Linking isn't configured."):
            return
        channel_id = _normalize_channel_id(local_id) if local_id else str(interaction.channel_id)
        summary = await self._linker.list_linked_channels(
            local_connector=self.connector_id, local_channel_id=channel_id
        )
        editor = await self._listing_editor(interaction, "channel", self._linker, channel_id)
        await self._send_linker_reply(interaction, summary, editor=editor)

    async def _handle_linked_users(self, interaction: discord.Interaction, local_id: discord.Member | None) -> None:
        if not await self._linker_configured(interaction, self._user_linker, "User linking isn't configured."):
            return
        if local_id is not None:
            summary = await self._user_linker.list_linked_users(
                local_connector=self.connector_id, local_user_id=str(local_id.id)
            )
            editor = await self._listing_editor(interaction, "user", self._user_linker, str(local_id.id))
        else:
            summary = await self._user_linker.list_linked_users()
            editor = None
        await self._send_linker_reply(interaction, summary, editor=editor)

    async def _handle_link_channel(
        self, interaction: discord.Interaction, service: str, external_id: str, local_id: str | None
    ) -> None:
        if not await self._linker_configured(interaction, self._linker, "Linking isn't configured."):
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
        # The actual anchor is `local_id` (an explicit destination override)
        # when given, else the invoking channel - matching link_channel's
        # own destination_channel_id resolution (services/discord_service
        # doesn't know the override's real name, so it falls back to the id
        # the same way ChannelLinker._resolve_name does for an unresolvable one).
        anchor_id = local_id or str(interaction.channel_id)
        anchor_name = anchor_id if local_id else getattr(interaction.channel, "name", anchor_id)
        await self._reply_linker_result(
            interaction,
            self._linker.link_channel(
                local_connector=self.connector_id,
                local_channel_id=str(interaction.channel_id),
                local_channel_name=getattr(interaction.channel, "name", str(interaction.channel_id)),
                source=service,
                source_id=external_id,
                destination_id=local_id,
            ),
            log_context="/link channel",
            editor=LinkEditorSpec(
                kind="channel", linker=self._linker, local_connector=self.connector_id,
                local_id=anchor_id, local_name=anchor_name, edited_connector=service,
            ),
        )

    def _invoking_category_id(self, interaction: discord.Interaction) -> str | None:
        category = getattr(interaction.channel, "category", None)
        return str(category.id) if category is not None else None

    async def _handle_linked_categories(
        self, interaction: discord.Interaction, local_id: str | None = None
    ) -> None:
        if not await self._linker_configured(interaction, self._category_linker, "Category linking isn't configured."):
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
        anchor_id = local_id or self._invoking_category_id(interaction)
        editor = await self._listing_editor(interaction, "category", self._category_linker, anchor_id)
        await self._send_linker_reply(interaction, summary, editor=editor)

    async def _handle_link_category(
        self, interaction: discord.Interaction, service: str, external_id: str, local_id: str | None
    ) -> None:
        if not await self._linker_configured(interaction, self._category_linker, "Category linking isn't configured."):
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
        anchor_id = local_id or str(category.id)
        anchor_name = local_id or category.name
        await self._reply_linker_result(
            interaction,
            self._category_linker.link_category(
                local_connector=self.connector_id,
                local_category_id=None if category is None else str(category.id),
                local_category_name="" if category is None else category.name,
                source=service,
                source_id=external_id,
                destination_id=local_id,
            ),
            log_context="/link category",
            editor=LinkEditorSpec(
                kind="category", linker=self._category_linker, local_connector=self.connector_id,
                local_id=anchor_id, local_name=anchor_name, edited_connector=service,
            ),
        )

    async def _handle_unlink_category(
        self, interaction: discord.Interaction, local_id: str | None = None, service: str | None = None
    ) -> None:
        if not await self._linker_configured(interaction, self._category_linker, "Category linking isn't configured."):
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
        await self._reply_linker_result(
            interaction,
            self._category_linker.unlink_category(
                local_connector=self.connector_id,
                local_category_id=self._invoking_category_id(interaction),
                local_category=local_id,
                destination=service,
            ),
            log_context="/unlink category",
        )

    async def _handle_mirror_category(
        self,
        interaction: discord.Interaction,
        service: str,
        local_id: str | None = None,
        new_name: str | None = None,
    ) -> None:
        if not await self._linker_configured(interaction, self._category_linker, "Category linking isn't configured."):
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
        editor = None
        if service.lower() == "all":
            coro = self._category_linker.mirror_category_all(**kwargs)
        else:
            coro = self._category_linker.mirror_category(destination=service, new_name=new_name, **kwargs)
            anchor_id = local_id or self._invoking_category_id(interaction)
            category = getattr(interaction.channel, "category", None)
            anchor_name = anchor_id if local_id or category is None else category.name
            editor = LinkEditorSpec(
                kind="category", linker=self._category_linker, local_connector=self.connector_id,
                local_id=anchor_id, local_name=anchor_name, edited_connector=service,
            )
        await self._reply_linker_result(
            interaction, coro, log_context="/mirror category", deferred=True, empty_fallback="Nothing to mirror.",
            editor=editor,
        )

    async def _handle_mirror_category_from(
        self, interaction: discord.Interaction, service: str, external_id: str, new_name: str | None = None
    ) -> None:
        """`/mirror category from <service> <external_id>`: create a local
        Category mirroring `service`'s, link them, and relocate/mirror its
        channels into the local Category."""
        if not await self._linker_configured(interaction, self._category_linker, "Category linking isn't configured."):
            return
        logger.info(
            "[discord:%s] %s ran /mirror category from service=%s external_id=%s",
            self.connector_id,
            interaction.user.id,
            service,
            external_id,
        )
        await interaction.response.defer(ephemeral=True, thinking=True)
        await self._reply_linker_result(
            interaction,
            self._category_linker.mirror_category_from(
                local_connector=self.connector_id, source=service, source_id=external_id, new_name=new_name
            ),
            log_context="/mirror category from",
            deferred=True,
            empty_fallback="Nothing to mirror.",
        )

    async def _handle_link_role(
        self, interaction: discord.Interaction, local_id: str, service: str, external_id: str
    ) -> None:
        if not await self._linker_configured(interaction, self._role_linker, "Role linking isn't configured."):
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
        await self._reply_linker_result(
            interaction,
            self._role_linker.link_role(
                local_connector=self.connector_id,
                local_role=local_id,
                source=service,
                source_role=external_id,
            ),
            log_context="/link-role",
            editor=LinkEditorSpec(
                kind="role", linker=self._role_linker, local_connector=self.connector_id,
                local_id=local_id, local_name=local_id, edited_connector=service,
            ),
        )

    async def _handle_unlink_role(
        self, interaction: discord.Interaction, local_id: str, service: str | None
    ) -> None:
        if not await self._linker_configured(interaction, self._role_linker, "Role linking isn't configured."):
            return
        local_id = _normalize_role_id(local_id)
        logger.info(
            "[discord:%s] %s ran /unlink-role local_id=%s service=%s",
            self.connector_id,
            interaction.user.id,
            local_id,
            service,
        )
        await self._reply_linker_result(
            interaction,
            self._role_linker.unlink_role(local_connector=self.connector_id, local_role=local_id, destination=service),
            log_context="/unlink-role",
        )

    async def _handle_linked_roles(
        self, interaction: discord.Interaction, local_id: str | None, service: str | None
    ) -> None:
        if not await self._linker_configured(interaction, self._role_linker, "Role linking isn't configured."):
            return
        normalized_id = _normalize_role_id(local_id) if local_id else None
        summary = await self._role_linker.list_linked_roles(
            local_connector=self.connector_id,
            local_role=normalized_id,
            service=service,
        )
        editor = (
            await self._listing_editor(interaction, "role", self._role_linker, normalized_id)
            if normalized_id
            else None
        )
        await self._send_linker_reply(interaction, summary, editor=editor)

    async def _handle_mirror_role(
        self,
        interaction: discord.Interaction,
        local_id: str | None,
        service: str,
        new_name: str | None = None,
    ) -> None:
        if not await self._linker_configured(interaction, self._role_linker, "Role linking isn't configured."):
            return
        if not local_id:
            await interaction.response.send_message("Which role? Pass a role id or name.", ephemeral=True)
            return
        local_id = _normalize_role_id(local_id)
        logger.info(
            "[discord:%s] %s ran /mirror-role local_id=%s service=%s",
            self.connector_id,
            interaction.user.id,
            local_id,
            service,
        )
        # Creating/matching the role on the target connector is a network
        # round-trip that can outrun Discord's 3s deadline - defer + followup.
        await interaction.response.defer(ephemeral=True, thinking=True)
        editor = None
        if service.lower() == "all":
            coro = self._role_linker.mirror_role_all(local_connector=self.connector_id, local_role=local_id)
        else:
            coro = self._role_linker.mirror_role(
                local_connector=self.connector_id, local_role=local_id, destination=service, new_name=new_name
            )
            editor = LinkEditorSpec(
                kind="role", linker=self._role_linker, local_connector=self.connector_id,
                local_id=local_id, local_name=local_id, edited_connector=service,
            )
        await self._reply_linker_result(
            interaction, coro, log_context="/mirror-role", deferred=True, empty_fallback="Nothing to mirror.",
            editor=editor,
        )

    async def _handle_mirror_role_from(
        self, interaction: discord.Interaction, service: str, external_id: str, new_name: str | None = None
    ) -> None:
        """`/mirror role from <service> <external_id>`: create-or-match a
        local role mirroring `service`'s role, and link them."""
        if not await self._linker_configured(interaction, self._role_linker, "Role linking isn't configured."):
            return
        external_id = _normalize_role_id(external_id)
        logger.info(
            "[discord:%s] %s ran /mirror role from service=%s external_id=%s",
            self.connector_id,
            interaction.user.id,
            service,
            external_id,
        )
        await interaction.response.defer(ephemeral=True, thinking=True)
        await self._reply_linker_result(
            interaction,
            self._role_linker.mirror_role_from(
                local_connector=self.connector_id, source=service, source_role=external_id, new_name=new_name
            ),
            log_context="/mirror role from",
            deferred=True,
            empty_fallback="Nothing to mirror.",
        )

    async def _handle_link_emote(
        self, interaction: discord.Interaction, service: str, external_id: str, local_id: str
    ) -> None:
        if not await self._linker_configured(interaction, self._emote_linker, "Linking isn't configured."):
            return
        logger.info(
            "[discord:%s] %s ran /link emote service=%s external_id=%s local_id=%s",
            self.connector_id,
            interaction.user.id,
            service,
            external_id,
            local_id,
        )
        await self._reply_linker_result(
            interaction,
            self._emote_linker.link_emote(
                local_connector=self.connector_id,
                local_id=local_id,
                source=service,
                source_id=external_id,
            ),
            log_context="/link emote",
            editor=LinkEditorSpec(
                kind="emote", linker=self._emote_linker, local_connector=self.connector_id,
                local_id=local_id, local_name=local_id, edited_connector=service,
            ),
        )

    async def _handle_unlink_emote(
        self, interaction: discord.Interaction, local_id: str, service: str | None
    ) -> None:
        if not await self._linker_configured(interaction, self._emote_linker, "Linking isn't configured."):
            return
        logger.info(
            "[discord:%s] %s ran /unlink emote local_id=%s service=%s",
            self.connector_id,
            interaction.user.id,
            local_id,
            service,
        )
        await self._reply_linker_result(
            interaction,
            self._emote_linker.unlink_emote(local_connector=self.connector_id, local_emote=local_id, destination=service),
            log_context="/unlink emote",
        )

    async def _handle_linked_emotes(
        self, interaction: discord.Interaction, local_id: str | None
    ) -> None:
        if not await self._linker_configured(interaction, self._emote_linker, "Linking isn't configured."):
            return
        summary = await self._emote_linker.list_linked_emotes(
            local_connector=self.connector_id, local_emote=local_id
        )
        editor = (
            await self._listing_editor(interaction, "emote", self._emote_linker, local_id) if local_id else None
        )
        await self._send_linker_reply(interaction, summary, editor=editor)

    async def _handle_mirror_emote(
        self,
        interaction: discord.Interaction,
        local_id: str | None,
        service: str,
        new_name: str | None = None,
    ) -> None:
        if not await self._linker_configured(interaction, self._emote_linker, "Linking isn't configured."):
            return
        if not local_id:
            await interaction.response.send_message("Which emote? Pass an emoji id or name.", ephemeral=True)
            return
        logger.info(
            "[discord:%s] %s ran /mirror emote local_id=%s service=%s",
            self.connector_id,
            interaction.user.id,
            local_id,
            service,
        )
        # Recreating the emoji on the target connector uploads its image -
        # comfortably past Discord's 3s deadline - so defer + followup.
        await interaction.response.defer(ephemeral=True, thinking=True)
        editor = None
        if service.lower() == "all":
            coro = self._emote_linker.mirror_emote_all(local_connector=self.connector_id, local_emote=local_id)
        else:
            coro = self._emote_linker.mirror_emote(
                local_connector=self.connector_id, local_emote=local_id, destination=service, new_name=new_name
            )
            editor = LinkEditorSpec(
                kind="emote", linker=self._emote_linker, local_connector=self.connector_id,
                local_id=local_id, local_name=local_id, edited_connector=service,
            )
        await self._reply_linker_result(
            interaction, coro, log_context="/mirror emote", deferred=True, empty_fallback="Nothing to mirror.",
            editor=editor,
        )

    async def _handle_mirror_emote_from(
        self, interaction: discord.Interaction, service: str, external_id: str, new_name: str | None = None
    ) -> None:
        """`/mirror emote from <service> <external_id>`: recreate-or-match
        `service`'s custom emoji locally, and link them."""
        if not await self._linker_configured(interaction, self._emote_linker, "Linking isn't configured."):
            return
        logger.info(
            "[discord:%s] %s ran /mirror emote from service=%s external_id=%s",
            self.connector_id,
            interaction.user.id,
            service,
            external_id,
        )
        await interaction.response.defer(ephemeral=True, thinking=True)
        await self._reply_linker_result(
            interaction,
            self._emote_linker.mirror_emote_from(
                local_connector=self.connector_id, source=service, source_emote=external_id, new_name=new_name
            ),
            log_context="/mirror emote from",
            deferred=True,
            empty_fallback="Nothing to mirror.",
        )

    async def _handle_link_user(
        self, interaction: discord.Interaction, service: str, external_id: str, local_id: discord.Member
    ) -> None:
        # local_id is a real discord.Member (picked from Discord's own
        # member search, not typed as free text) specifically so this can't
        # end up linked to a mistyped/malformed id or a bare "@name" - see
        # LinkError-free "Unknown User"/`<@@name>` mangling that caused
        # further downstream once such a bad id was already on file.
        if not await self._linker_configured(interaction, self._user_linker, "User linking isn't configured."):
            return
        logger.info(
            "[discord:%s] %s ran /link user service=%s external_id=%s local_id=%s",
            self.connector_id,
            interaction.user.id,
            service,
            external_id,
            local_id.id,
        )
        await self._reply_linker_result(
            interaction,
            self._user_linker.link_user(
                local_connector=self.connector_id,
                local_user_id=str(local_id.id),
                source=service,
                source_user_id=external_id,
            ),
            log_context="/link user",
            editor=LinkEditorSpec(
                kind="user", linker=self._user_linker, local_connector=self.connector_id,
                local_id=str(local_id.id), local_name=local_id.display_name, edited_connector=service,
            ),
        )

    async def _handle_mirror_channel(
        self,
        interaction: discord.Interaction,
        service: str,
        local_id: str | None,
        new_name: str | None = None,
        category: str | None = None,
        with_history: bool = False,
        history_limit: str | None = None,
    ) -> None:
        if not await self._linker_configured(interaction, self._linker, "Linking isn't configured."):
            return
        if category and service.lower() == "all":
            await interaction.response.send_message(
                "A destination Category can only be set when mirroring to a single connector, not 'all'.",
                ephemeral=True,
            )
            return
        if with_history and service.lower() == "all":
            await interaction.response.send_message(
                "'with history' can only be used when mirroring to a single connector, not 'all'.",
                ephemeral=True,
            )
            return
        if local_id is not None:
            channel_id = _normalize_channel_id(local_id)
            channel_name = await self.get_channel_name(channel_id) or channel_id
        else:
            channel_id = str(interaction.channel_id)
            channel_name = getattr(interaction.channel, "name", channel_id)
            # The bot can be handed an interaction from a channel it can't
            # actually see; mirroring it then names the new channel after
            # Discord's `__hidden__` placeholder (issue #33). app_permissions
            # is the interaction channel's computed perms for this app - no
            # cache needed - so it catches the current-channel case even when
            # the channel never reached the guild cache.
            if not interaction.app_permissions.view_channel:
                await interaction.response.send_message(
                    "I can't see this channel, so I can't mirror it - grant me access to it first.",
                    ephemeral=True,
                )
                return
        channel_category = await self.get_channel_category_name(channel_id)
        logger.info(
            "[discord:%s] %s ran /mirror channel service=%s local_id=%s",
            self.connector_id,
            interaction.user.id,
            service,
            channel_id,
        )
        # Mirroring creates channels + webhooks on the target connector, which
        # runs well past Discord's 3s interaction-response deadline - defer up
        # front and reply via followup so the token doesn't expire mid-run.
        await interaction.response.defer(ephemeral=True, thinking=True)
        editor = None
        if service.lower() == "all":
            coro = self._linker.mirror_channel_all(
                local_connector=self.connector_id,
                local_channel_id=channel_id,
                local_channel_name=channel_name,
                local_channel_category=channel_category,
            )
        else:
            coro = self._linker.mirror_channel(
                local_connector=self.connector_id,
                local_channel_id=channel_id,
                local_channel_name=channel_name,
                destination=service,
                local_channel_category=channel_category,
                destination_category=category,
                new_name=new_name,
                with_history=with_history,
                history_limit=history_limit,
            )
            editor = LinkEditorSpec(
                kind="channel", linker=self._linker, local_connector=self.connector_id,
                local_id=channel_id, local_name=channel_name, edited_connector=service,
            )
        await self._reply_linker_result(
            interaction, coro, log_context="/mirror channel", deferred=True, empty_fallback="Nothing to mirror.",
            editor=editor,
        )

    async def _handle_transfer_history(
        self,
        interaction: discord.Interaction,
        direction: str,
        service: str,
        external_channel: str,
        local_channel: str | None,
        history_limit: str | None,
    ) -> None:
        """`/import` / `/export` (issue #161): copy `service`'s
        `external_channel` history into `local_channel`, or the reverse.
        `local_channel` defaults to the channel the command was run in."""
        if not await self._linker_configured(interaction, self._linker, "Linking isn't configured."):
            return
        if local_channel is not None:
            local_channel_id = _normalize_channel_id(local_channel)
        else:
            local_channel_id = str(interaction.channel_id)
            # Same current-channel check as /mirror channel (issue #33).
            if not interaction.app_permissions.view_channel:
                await interaction.response.send_message(
                    f"I can't see this channel, so I can't {direction} its history - grant me access to it first.",
                    ephemeral=True,
                )
                return
        logger.info(
            "[discord:%s] %s ran /%s service=%s external_channel=%s local_channel=%s",
            self.connector_id,
            interaction.user.id,
            direction,
            service,
            external_channel,
            local_channel_id,
        )
        # A history transfer runs far past the 3s response deadline.
        await interaction.response.defer(ephemeral=True, thinking=True)
        coro = self._linker.transfer_history(
            local_connector=self.connector_id,
            service=service,
            external_channel_id=_normalize_channel_id(external_channel),
            local_channel_id=local_channel_id,
            direction=direction,
            history_limit=history_limit,
        )
        await self._reply_linker_result(interaction, coro, log_context=f"/{direction}", deferred=True)

    async def _handle_mirror_channel_from(
        self,
        interaction: discord.Interaction,
        service: str,
        external_id: str,
        new_name: str | None = None,
        category: str | None = None,
        with_history: bool = False,
        history_limit: str | None = None,
    ) -> None:
        """`/mirror channel from <service> <external_id>`: create a local
        channel mirroring `service`'s `external_id` and link them, placing it
        in the local counterpart of the source channel's linked Category - or
        in `category` (a local Category id/name), if given, which overrides
        that (issue #75)."""
        if not await self._linker_configured(interaction, self._linker, "Linking isn't configured."):
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
        await self._reply_linker_result(
            interaction,
            self._linker.mirror_channel_from(
                local_connector=self.connector_id,
                source=service,
                source_id=external_id,
                new_name=new_name,
                local_category=category,
                with_history=with_history,
                history_limit=history_limit,
            ),
            log_context="/mirror channel from",
            deferred=True,
            empty_fallback="Nothing to mirror.",
        )

    async def _handle_unlink_channel(
        self, interaction: discord.Interaction, service: str | None, local_id: str | None
    ) -> None:
        if not await self._linker_configured(interaction, self._linker, "Linking isn't configured."):
            return
        channel_id = _normalize_channel_id(local_id) if local_id is not None else str(interaction.channel_id)
        logger.info(
            "[discord:%s] %s ran /unlink channel service=%s local_id=%s",
            self.connector_id,
            interaction.user.id,
            service,
            channel_id,
        )
        await self._reply_linker_result(
            interaction,
            self._linker.unlink_channel(local_connector=self.connector_id, local_channel_id=channel_id, destination=service),
            log_context="/unlink channel",
        )

    async def _handle_unlink_user(
        self, interaction: discord.Interaction, service: str | None, local_id: discord.Member | None
    ) -> None:
        if not await self._linker_configured(interaction, self._user_linker, "User linking isn't configured."):
            return
        target = local_id or interaction.user
        logger.info(
            "[discord:%s] %s ran /unlink user service=%s local_id=%s",
            self.connector_id,
            interaction.user.id,
            service,
            target.id,
        )
        await self._reply_linker_result(
            interaction,
            self._user_linker.unlink_user(local_connector=self.connector_id, local_user_id=str(target.id), destination=service),
            log_context="/unlink user",
        )

    async def _handle_whitelist(
        self, interaction: discord.Interaction, action: str, service: str, bot: str
    ) -> None:
        if not await self._linker_configured(interaction, self._bot_whitelist, "Bot whitelisting isn't configured."):
            return
        target_connector = self.connector_id if service == "local" else service
        logger.info(
            "[discord:%s] %s ran /whitelist action=%s service=%s bot=%s",
            self.connector_id,
            interaction.user.id,
            action,
            target_connector,
            bot,
        )
        if action == "remove":
            coro = self._bot_whitelist.remove_bot(target_connector=target_connector, bot_ref=bot)
        else:
            coro = self._bot_whitelist.whitelist_bot(
                target_connector=target_connector, bot_ref=bot, added_by=str(interaction.user.id)
            )
        await self._reply_linker_result(interaction, coro, log_context="/whitelist")

    async def _handle_whitelisted(self, interaction: discord.Interaction, service: str) -> None:
        if not await self._linker_configured(interaction, self._bot_whitelist, "Bot whitelisting isn't configured."):
            return
        target_connector = self.connector_id if service == "local" else service
        summary = await self._bot_whitelist.list_whitelisted_bots(target_connector=target_connector)
        await interaction.response.send_message(summary, ephemeral=True)
