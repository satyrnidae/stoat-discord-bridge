"""The `/link`, `/unlink`, `/linked`, `/mirror` command handlers for Stoat.

These are the Mongo-backed half: every method here is what
`commands.build_command_tree`'s callbacks forward to, and each one drives a
shared linker (`ChannelLinker` / `CategoryLinker` / `EmoteLinker` /
`RoleLinker` / `UserLinker`) that reads and writes the cross-connector
mapping collections. `_is_admin` is the Manage-Server execution gate they
all check. Composed into `StoatSenderService`.
"""

from __future__ import annotations

import logging

from stoat_discord_bridge.admin_commands import LinkError
from stoat_discord_bridge.services.stoat_service.formatting import _channel_category

logger = logging.getLogger(__name__)


class StoatLinkingMixin:
    """Command-handler half of `StoatSenderService`."""

    async def _linked_channels(self, ctx, local_id: str | None = None) -> None:
        """`/linked channels [local_id|name]` - read-only. Defaults to the
        invoking channel."""
        if self._linker is None:
            await self._reply(ctx, "Linking isn't configured.")
            return
        target = local_id if local_id else str(ctx.channel.id)
        summary = await self._linker.list_linked_channels(
            local_connector=self.connector_id, local_channel_id=target
        )
        await self._reply(ctx, summary)

    async def _linked_categories(self, ctx, local_id: str | None = None) -> None:
        """`/linked categories [<local_id|name>]` - read-only."""
        if self._category_linker is None:
            await self._reply(ctx, "Category linking isn't configured.")
            return
        category = _channel_category(ctx.channel)
        if local_id is None and category is None:
            await self._reply(ctx, "This channel isn't in a Category.")
            return
        summary = await self._category_linker.list_linked_categories(
            local_connector=self.connector_id,
            local_category_id=str(category.id) if category is not None else None,
            local_category=local_id,
        )
        await self._reply(ctx, summary)

    async def _linked_users(self, ctx, local_id: str | None = None) -> None:
        """`/linked users [local_id|name]`: with no argument, lists every
        cross-connector user link (for debugging); given a Stoat user id or
        display name, shows just that identity's link. No permission gate -
        read-only, same as /status and /linked channels."""
        if self._user_linker is None:
            await self._reply(ctx, "User linking isn't configured.")
            return
        if local_id:
            summary = await self._user_linker.list_linked_users(
                local_connector=self.connector_id, local_user_id=local_id
            )
        else:
            summary = await self._user_linker.list_linked_users()
        await self._reply(ctx, summary)

    async def _link_channel(self, ctx, service: str, external_id: str, local_id: str | None = None) -> None:
        """`/link channel <service> <external_id|name> [local_id|name]`: the
        local side defaults to the invoking channel."""
        if not self._is_admin(ctx.message):
            await self._reply(ctx, "You need the Manage Server permission to do that.")
            return
        if self._linker is None:
            await self._reply(ctx, "Linking isn't configured.")
            return
        logger.info(
            "[stoat:%s] %s ran /link channel local_id=%s service=%s external_id=%s",
            self.connector_id,
            ctx.author_id,
            local_id,
            service,
            external_id,
        )
        try:
            summary = await self._linker.link_channel(
                local_connector=self.connector_id,
                local_channel_id=str(ctx.channel.id),
                local_channel_name=getattr(ctx.channel, "name", str(ctx.channel.id)),
                source=service,
                source_id=external_id,
                destination_id=local_id,
            )
        except LinkError as exc:
            logger.info("[stoat:%s] /link channel rejected: %s", self.connector_id, exc)
            await self._reply(ctx, str(exc))
            return
        await self._reply(ctx, summary)

    async def _link_category(self, ctx, service: str, external_id: str, local_id: str | None = None) -> None:
        """`/link category <service> <external_id|name> [<local_id|name>]`:
        links the invoking channel's Category (or `local_id`'s Category, if
        given) to `external_id`'s Category on `service`. Once linked, a new
        channel appearing in either Category auto-syncs onto the other."""
        if not self._is_admin(ctx.message):
            await self._reply(ctx, "You need the Manage Server permission to do that.")
            return
        if self._category_linker is None:
            await self._reply(ctx, "Category linking isn't configured.")
            return
        category = _channel_category(ctx.channel)
        if category is None and local_id is None:
            await self._reply(ctx, "This channel isn't in a Category.")
            return
        logger.info(
            "[stoat:%s] %s ran /link category service=%s external_id=%s local_id=%s",
            self.connector_id,
            ctx.author_id,
            service,
            external_id,
            local_id,
        )
        try:
            summary = await self._category_linker.link_category(
                local_connector=self.connector_id,
                local_category_id=str(category.id) if category is not None else None,
                local_category_name=category.title if category is not None else "",
                source=service,
                source_id=external_id,
                destination_id=local_id,
            )
        except LinkError as exc:
            logger.info("[stoat:%s] /link category rejected: %s", self.connector_id, exc)
            await self._reply(ctx, str(exc))
            return
        await self._reply(ctx, summary)

    async def _link_emote(self, ctx, service: str, external_id: str, local_id: str) -> None:
        """`/link emote <service> <external_id|name> <local_id|name>`."""
        if not self._is_admin(ctx.message):
            await self._reply(ctx, "You need the Manage Server permission to do that.")
            return
        if self._emote_linker is None:
            await self._reply(ctx, "Linking isn't configured.")
            return
        logger.info(
            "[stoat:%s] %s ran /link emote service=%s external_id=%s local_id=%s",
            self.connector_id,
            ctx.author_id,
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
            logger.info("[stoat:%s] /link emote rejected: %s", self.connector_id, exc)
            await self._reply(ctx, str(exc))
            return
        await self._reply(ctx, summary)

    async def _unlink_emote(self, ctx, local_id: str, service: str | None = None) -> None:
        """`/unlink emote <local_id|name> [<service>|all]`."""
        if not self._is_admin(ctx.message):
            await self._reply(ctx, "You need the Manage Server permission to do that.")
            return
        if self._emote_linker is None:
            await self._reply(ctx, "Linking isn't configured.")
            return
        try:
            summary = await self._emote_linker.unlink_emote(
                local_connector=self.connector_id, local_emote=local_id, destination=service
            )
        except LinkError as exc:
            logger.info("[stoat:%s] /unlink emote rejected: %s", self.connector_id, exc)
            await self._reply(ctx, str(exc))
            return
        await self._reply(ctx, summary)

    async def _linked_emotes(self, ctx, local_id: str | None = None, service: str | None = None) -> None:
        """`/linked emotes [<local_id|name>] [<service>|all]` - read-only."""
        if self._emote_linker is None:
            await self._reply(ctx, "Linking isn't configured.")
            return
        summary = await self._emote_linker.list_linked_emotes(
            local_connector=self.connector_id, local_emote=local_id, service=service
        )
        await self._reply(ctx, summary)

    async def _mirror_emote(
        self, ctx, local_id: str | None = None, service: str | None = None, new_name: str | None = None
    ) -> None:
        """`/mirror emote to <local_id|name> [<service>|all] [new_name]`."""
        if not self._is_admin(ctx.message):
            await self._reply(ctx, "You need the Manage Server permission to do that.")
            return
        if self._emote_linker is None:
            await self._reply(ctx, "Linking isn't configured.")
            return
        if not local_id:
            await self._reply(ctx, "Which emote? Pass an emoji id or name.")
            return
        try:
            if service is None or service.lower() == "all":
                summary = await self._emote_linker.mirror_emote_all(
                    local_connector=self.connector_id, local_emote=local_id
                )
            else:
                summary = await self._emote_linker.mirror_emote(
                    local_connector=self.connector_id, local_emote=local_id, destination=service, new_name=new_name
                )
        except LinkError as exc:
            logger.info("[stoat:%s] /mirror emote rejected: %s", self.connector_id, exc)
            await self._reply(ctx, str(exc))
            return
        await self._reply(ctx, summary)

    async def _mirror_emote_from(
        self, ctx, service: str, external_id: str, new_name: str | None = None
    ) -> None:
        """`/mirror emote from <service> <external_id|name> [new_name]`:
        recreate-or-match `service`'s custom emoji locally and link them."""
        if not self._is_admin(ctx.message):
            await self._reply(ctx, "You need the Manage Server permission to do that.")
            return
        if self._emote_linker is None:
            await self._reply(ctx, "Linking isn't configured.")
            return
        try:
            summary = await self._emote_linker.mirror_emote_from(
                local_connector=self.connector_id, source=service, source_emote=external_id, new_name=new_name
            )
        except LinkError as exc:
            logger.info("[stoat:%s] /mirror emote from rejected: %s", self.connector_id, exc)
            await self._reply(ctx, str(exc))
            return
        await self._reply(ctx, summary)

    async def _link_user(self, ctx, service: str, external_id: str, local_id: str) -> None:
        if not self._is_admin(ctx.message):
            await self._reply(ctx, "You need the Manage Server permission to do that.")
            return
        if self._user_linker is None:
            await self._reply(ctx, "User linking isn't configured.")
            return
        logger.info(
            "[stoat:%s] %s ran /link user service=%s external_id=%s local_id=%s",
            self.connector_id,
            ctx.author_id,
            service,
            external_id,
            local_id,
        )
        try:
            summary = await self._user_linker.link_user(
                local_connector=self.connector_id,
                local_user_id=local_id,
                source=service,
                source_user_id=external_id,
            )
        except LinkError as exc:
            logger.info("[stoat:%s] /link user rejected: %s", self.connector_id, exc)
            await self._reply(ctx, str(exc))
            return
        await self._reply(ctx, summary)

    async def _mirror_channel(
        self,
        ctx,
        local_id: str | None = None,
        service: str | None = None,
        new_name: str | None = None,
        category: str | None = None,
    ) -> None:
        """`/mirror channel [local_id|name] [<service>|all] [new_name] [category:<id|name>]`:
        local_id defaults to the invoking channel; service defaults to "all".
        `category` (a Category id/name on the target service) overrides linked
        Categories and needs a single service (issue #75)."""
        if not self._is_admin(ctx.message):
            await self._reply(ctx, "You need the Manage Server permission to do that.")
            return
        if category and (service is None or service.lower() == "all"):
            await self._reply(
                ctx, "A destination Category can only be set when mirroring to a single service, not 'all'."
            )
            return
        if local_id:
            channel_id = channel_name = local_id  # explicit id/name - no way to resolve its real display name
        else:
            channel_id = str(ctx.channel.id)
            channel_name = getattr(ctx.channel, "name", channel_id)

        if self._linker is None:
            await self._reply(ctx, "Linking isn't configured.")
            return
        channel_category = await self.get_channel_category_name(channel_id)
        logger.info(
            "[stoat:%s] %s ran /mirror channel local_id=%s service=%s",
            self.connector_id,
            ctx.author_id,
            channel_id,
            service,
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
                    destination_category=category,
                    new_name=new_name,
                )
        except LinkError as exc:
            logger.info("[stoat:%s] /mirror channel rejected: %s", self.connector_id, exc)
            await self._reply(ctx, str(exc))
            return
        await self._reply(ctx, summary)

    async def _mirror_channel_from(
        self, ctx, service: str, external_id: str, new_name: str | None = None, category: str | None = None
    ) -> None:
        """`/mirror channel from <service> <external_id|name> [new_name] [category:<id|name>]`:
        create a local channel mirroring `service`'s and link them, landing it in
        the local counterpart of the source channel's linked Category - or in
        `category` (a local Category id/name), if given, which overrides that
        (issue #75)."""
        if not self._is_admin(ctx.message):
            await self._reply(ctx, "You need the Manage Server permission to do that.")
            return
        if self._linker is None:
            await self._reply(ctx, "Linking isn't configured.")
            return
        try:
            summary = await self._linker.mirror_channel_from(
                local_connector=self.connector_id,
                source=service,
                source_id=external_id,
                new_name=new_name,
                local_category=category,
            )
        except LinkError as exc:
            logger.info("[stoat:%s] /mirror channel from rejected: %s", self.connector_id, exc)
            await self._reply(ctx, str(exc))
            return
        await self._reply(ctx, summary)

    async def _unlink_channel(self, ctx, local_id: str | None = None, service: str | None = None) -> None:
        """`/unlink channel [local_id|name] [<service>|all]`: local_id
        defaults to the invoking channel; service defaults to "all"
        (dissolving the whole bridge group)."""
        if not self._is_admin(ctx.message):
            await self._reply(ctx, "You need the Manage Server permission to do that.")
            return
        channel_id = local_id if local_id else str(ctx.channel.id)

        if self._linker is None:
            await self._reply(ctx, "Linking isn't configured.")
            return
        logger.info(
            "[stoat:%s] %s ran /unlink channel local_id=%s service=%s",
            self.connector_id,
            ctx.author_id,
            channel_id,
            service,
        )
        try:
            summary = await self._linker.unlink_channel(
                local_connector=self.connector_id, local_channel_id=channel_id, destination=service
            )
        except LinkError as exc:
            logger.info("[stoat:%s] /unlink channel rejected: %s", self.connector_id, exc)
            await self._reply(ctx, str(exc))
            return
        await self._reply(ctx, summary)

    async def _unlink_category(self, ctx, local_id: str | None = None, service: str | None = None) -> None:
        """`/unlink category [<local_id|name>] [<service>|all]`: local Category
        defaults to the invoking channel's own; service defaults to "all"
        (dissolving the whole bridge group)."""
        if not self._is_admin(ctx.message):
            await self._reply(ctx, "You need the Manage Server permission to do that.")
            return
        if self._category_linker is None:
            await self._reply(ctx, "Category linking isn't configured.")
            return
        category = _channel_category(ctx.channel)
        if local_id is None and category is None:
            await self._reply(ctx, "This channel isn't in a Category.")
            return
        logger.info(
            "[stoat:%s] %s ran /unlink category local=%s service=%s",
            self.connector_id,
            ctx.author_id,
            local_id,
            service,
        )
        try:
            summary = await self._category_linker.unlink_category(
                local_connector=self.connector_id,
                local_category_id=str(category.id) if category is not None else None,
                local_category=local_id,
                destination=service,
            )
        except LinkError as exc:
            logger.info("[stoat:%s] /unlink category rejected: %s", self.connector_id, exc)
            await self._reply(ctx, str(exc))
            return
        await self._reply(ctx, summary)

    async def _mirror_category(
        self, ctx, local_id: str | None = None, service: str | None = None, new_name: str | None = None
    ) -> None:
        """`/mirror category [<local_id|name>] [<service>|all] [new_name]`."""
        if not self._is_admin(ctx.message):
            await self._reply(ctx, "You need the Manage Server permission to do that.")
            return
        if self._category_linker is None:
            await self._reply(ctx, "Category linking isn't configured.")
            return
        category = _channel_category(ctx.channel)
        if local_id is None and category is None:
            await self._reply(ctx, "This channel isn't in a Category.")
            return
        kwargs = dict(
            local_connector=self.connector_id,
            local_category_id=str(category.id) if category is not None else None,
            local_category=local_id,
            local_category_name=category.title if category is not None else None,
        )
        try:
            if service is None or service.lower() == "all":
                summary = await self._category_linker.mirror_category_all(**kwargs)
            else:
                summary = await self._category_linker.mirror_category(
                    destination=service, new_name=new_name, **kwargs
                )
        except LinkError as exc:
            logger.info("[stoat:%s] /mirror category rejected: %s", self.connector_id, exc)
            await self._reply(ctx, str(exc))
            return
        await self._reply(ctx, summary or "Nothing to mirror.")

    async def _mirror_category_from(
        self, ctx, service: str, external_id: str, new_name: str | None = None
    ) -> None:
        """`/mirror category from <service> <external_id|name> [new_name]`: create
        a local Category mirroring `service`'s, link them, and relocate/mirror
        its channels into the local Category."""
        if not self._is_admin(ctx.message):
            await self._reply(ctx, "You need the Manage Server permission to do that.")
            return
        if self._category_linker is None:
            await self._reply(ctx, "Category linking isn't configured.")
            return
        try:
            summary = await self._category_linker.mirror_category_from(
                local_connector=self.connector_id, source=service, source_id=external_id, new_name=new_name
            )
        except LinkError as exc:
            logger.info("[stoat:%s] /mirror category from rejected: %s", self.connector_id, exc)
            await self._reply(ctx, str(exc))
            return
        await self._reply(ctx, summary or "Nothing to mirror.")

    async def _unlink_user(self, ctx, service: str | None = None, local_id: str | None = None) -> None:
        """`/unlink user [service|all] [local_id|name]`: service defaults
        to "all" (dissolving the whole link group); local_id defaults to the
        invoking user themselves."""
        if not self._is_admin(ctx.message):
            await self._reply(ctx, "You need the Manage Server permission to do that.")
            return
        target = local_id if local_id else str(ctx.author_id)

        if self._user_linker is None:
            await self._reply(ctx, "User linking isn't configured.")
            return
        logger.info(
            "[stoat:%s] %s ran /unlink user service=%s local_id=%s",
            self.connector_id,
            ctx.author_id,
            service,
            target,
        )
        try:
            summary = await self._user_linker.unlink_user(
                local_connector=self.connector_id, local_user_id=target, destination=service
            )
        except LinkError as exc:
            logger.info("[stoat:%s] /unlink user rejected: %s", self.connector_id, exc)
            await self._reply(ctx, str(exc))
            return
        await self._reply(ctx, summary)

    async def _link_role(self, ctx, local_id: str, service: str, external_id: str) -> None:
        """`/link role <local_id|name> <service> <external_id|name>`."""
        if not self._is_admin(ctx.message):
            await self._reply(ctx, "You need the Manage Server permission to do that.")
            return
        if self._role_linker is None:
            await self._reply(ctx, "Role linking isn't configured.")
            return
        logger.info(
            "[stoat:%s] %s ran /link role local=%s service=%s external=%s",
            self.connector_id,
            ctx.author_id,
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
            logger.info("[stoat:%s] /link role rejected: %s", self.connector_id, exc)
            await self._reply(ctx, str(exc))
            return
        await self._reply(ctx, summary)

    async def _unlink_role(self, ctx, local_id: str, service: str | None = None) -> None:
        """`/unlink role <local_id|name> [<service>|all]`."""
        if not self._is_admin(ctx.message):
            await self._reply(ctx, "You need the Manage Server permission to do that.")
            return
        if self._role_linker is None:
            await self._reply(ctx, "Role linking isn't configured.")
            return
        try:
            summary = await self._role_linker.unlink_role(
                local_connector=self.connector_id, local_role=local_id, destination=service
            )
        except LinkError as exc:
            logger.info("[stoat:%s] /unlink role rejected: %s", self.connector_id, exc)
            await self._reply(ctx, str(exc))
            return
        await self._reply(ctx, summary)

    async def _linked_roles(self, ctx, local_id: str | None = None, service: str | None = None) -> None:
        """`/linked roles [<local_id|name>] [<service>|all]` - read-only."""
        if self._role_linker is None:
            await self._reply(ctx, "Role linking isn't configured.")
            return
        summary = await self._role_linker.list_linked_roles(
            local_connector=self.connector_id, local_role=local_id, service=service
        )
        await self._reply(ctx, summary)

    async def _mirror_role(
        self, ctx, local_id: str | None = None, service: str | None = None, new_name: str | None = None
    ) -> None:
        """`/mirror role to <local_id|name> [<service>|all] [new_name]`."""
        if not self._is_admin(ctx.message):
            await self._reply(ctx, "You need the Manage Server permission to do that.")
            return
        if self._role_linker is None:
            await self._reply(ctx, "Role linking isn't configured.")
            return
        if not local_id:
            await self._reply(ctx, "Which role? Pass a role id or name.")
            return
        try:
            if service is None or service.lower() == "all":
                summary = await self._role_linker.mirror_role_all(
                    local_connector=self.connector_id, local_role=local_id
                )
            else:
                summary = await self._role_linker.mirror_role(
                    local_connector=self.connector_id, local_role=local_id, destination=service, new_name=new_name
                )
        except LinkError as exc:
            logger.info("[stoat:%s] /mirror role rejected: %s", self.connector_id, exc)
            await self._reply(ctx, str(exc))
            return
        await self._reply(ctx, summary)

    async def _mirror_role_from(
        self, ctx, service: str, external_id: str, new_name: str | None = None
    ) -> None:
        """`/mirror role from <service> <external_id|name> [new_name]`:
        create-or-match a local role mirroring `service`'s and link them."""
        if not self._is_admin(ctx.message):
            await self._reply(ctx, "You need the Manage Server permission to do that.")
            return
        if self._role_linker is None:
            await self._reply(ctx, "Role linking isn't configured.")
            return
        try:
            summary = await self._role_linker.mirror_role_from(
                local_connector=self.connector_id, source=service, source_role=external_id, new_name=new_name
            )
        except LinkError as exc:
            logger.info("[stoat:%s] /mirror role from rejected: %s", self.connector_id, exc)
            await self._reply(ctx, str(exc))
            return
        await self._reply(ctx, summary)

    def _is_admin(self, message) -> bool:
        """True if the command author has Stoat's Manage Server permission
        (mirrors the ``manage_guild`` default on Discord's command tree).
        Server owners always pass; a permissions-cache miss (or any other
        error) fails closed."""
        try:
            member = message.author_as_member
        except Exception:
            return False
        if member is None:
            return False
        try:
            server = member.get_server()
            if server is not None and getattr(server, "owner_id", None) == member.id:
                return True
        except Exception:
            pass
        try:
            return bool(member.server_permissions.manage_server)
        except Exception:
            return False
