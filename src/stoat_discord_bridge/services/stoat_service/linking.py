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
from collections.abc import Awaitable

from stoat_discord_bridge.admin_commands import LinkError
from stoat_discord_bridge.services.stoat_service.formatting import _channel_category

logger = logging.getLogger(__name__)


class StoatLinkingMixin:
    """Command-handler half of `StoatSenderService`."""

    async def _linker_configured(self, ctx, linker: object | None, message: str) -> bool:
        """True if `linker` is configured; otherwise replies `message` and
        returns False, so a handler opens with `if not await
        self._linker_configured(ctx, self._x_linker, "...isn't
        configured."): return` instead of repeating the
        None-check/reply/return three-liner (issue #106)."""
        if linker is not None:
            return True
        await self._reply(ctx, message)
        return False

    async def _require_admin(self, ctx) -> bool:
        """True if the invoking user has Stoat's Manage Server permission;
        otherwise replies the standard rejection message and returns False,
        so a mutating handler opens with `if not await
        self._require_admin(ctx): return` instead of repeating the
        `_is_admin`/reply/return three-liner (issue #106, item 7's "at
        minimum" fix - the fully declarative gate Discord's `app_commands`
        enjoys, closing the actual safety gap of a handler that forgets this
        call entirely, is left for a follow-up: it needs verifying against a
        live server how `stoat.ext.commands`' check decorators interact with
        this module's `_compat.py` patches, which unit tests against the fake
        client can't exercise)."""
        if self._is_admin(ctx.message):
            return True
        await self._reply(ctx, "You need the Manage Server permission to do that.")
        return False

    async def _reply_linker_result(
        self, ctx, coro: Awaitable[str], *, log_context: str, empty_fallback: str | None = None
    ) -> None:
        """The `try: summary = await <linker call> / except LinkError: log +
        reply(str(exc)) / else: reply(summary)` shape every mutating handler
        ends with. `empty_fallback` (the mirror handlers' "Nothing to
        mirror.") is substituted for a falsy `summary`, matching each
        handler's own `summary or "..."` it used to write inline."""
        try:
            summary = await coro
        except LinkError as exc:
            logger.info("[stoat:%s] %s rejected: %s", self.connector_id, log_context, exc)
            await self._reply(ctx, str(exc))
            return
        await self._reply(ctx, summary if empty_fallback is None else (summary or empty_fallback))

    async def _linked_channels(self, ctx, local_id: str | None = None) -> None:
        """`/linked channels [local_id|name]` - read-only. Defaults to the
        invoking channel."""
        if not await self._linker_configured(ctx, self._linker, "Linking isn't configured."):
            return
        target = local_id if local_id else str(ctx.channel.id)
        summary = await self._linker.list_linked_channels(
            local_connector=self.connector_id, local_channel_id=target
        )
        await self._reply(ctx, summary)

    async def _linked_categories(self, ctx, local_id: str | None = None) -> None:
        """`/linked categories [<local_id|name>]` - read-only."""
        if not await self._linker_configured(ctx, self._category_linker, "Category linking isn't configured."):
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
        if not await self._linker_configured(ctx, self._user_linker, "User linking isn't configured."):
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
        if not await self._require_admin(ctx):
            return
        if not await self._linker_configured(ctx, self._linker, "Linking isn't configured."):
            return
        logger.info(
            "[stoat:%s] %s ran /link channel local_id=%s service=%s external_id=%s",
            self.connector_id,
            ctx.author_id,
            local_id,
            service,
            external_id,
        )
        await self._reply_linker_result(
            ctx,
            self._linker.link_channel(
                local_connector=self.connector_id,
                local_channel_id=str(ctx.channel.id),
                local_channel_name=getattr(ctx.channel, "name", str(ctx.channel.id)),
                source=service,
                source_id=external_id,
                destination_id=local_id,
            ),
            log_context="/link channel",
        )

    async def _link_category(self, ctx, service: str, external_id: str, local_id: str | None = None) -> None:
        """`/link category <service> <external_id|name> [<local_id|name>]`:
        links the invoking channel's Category (or `local_id`'s Category, if
        given) to `external_id`'s Category on `service`. Once linked, a new
        channel appearing in either Category auto-syncs onto the other."""
        if not await self._require_admin(ctx):
            return
        if not await self._linker_configured(ctx, self._category_linker, "Category linking isn't configured."):
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
        await self._reply_linker_result(
            ctx,
            self._category_linker.link_category(
                local_connector=self.connector_id,
                local_category_id=str(category.id) if category is not None else None,
                local_category_name=category.title if category is not None else "",
                source=service,
                source_id=external_id,
                destination_id=local_id,
            ),
            log_context="/link category",
        )

    async def _link_emote(self, ctx, service: str, external_id: str, local_id: str) -> None:
        """`/link emote <service> <external_id|name> <local_id|name>`."""
        if not await self._require_admin(ctx):
            return
        if not await self._linker_configured(ctx, self._emote_linker, "Linking isn't configured."):
            return
        logger.info(
            "[stoat:%s] %s ran /link emote service=%s external_id=%s local_id=%s",
            self.connector_id,
            ctx.author_id,
            service,
            external_id,
            local_id,
        )
        await self._reply_linker_result(
            ctx,
            self._emote_linker.link_emote(
                local_connector=self.connector_id,
                local_id=local_id,
                source=service,
                source_id=external_id,
            ),
            log_context="/link emote",
        )

    async def _unlink_emote(self, ctx, local_id: str, service: str | None = None) -> None:
        """`/unlink emote <local_id|name> [<service>|all]`."""
        if not await self._require_admin(ctx):
            return
        if not await self._linker_configured(ctx, self._emote_linker, "Linking isn't configured."):
            return
        await self._reply_linker_result(
            ctx,
            self._emote_linker.unlink_emote(local_connector=self.connector_id, local_emote=local_id, destination=service),
            log_context="/unlink emote",
        )

    async def _linked_emotes(self, ctx, local_id: str | None = None, service: str | None = None) -> None:
        """`/linked emotes [<local_id|name>] [<service>|all]` - read-only."""
        if not await self._linker_configured(ctx, self._emote_linker, "Linking isn't configured."):
            return
        summary = await self._emote_linker.list_linked_emotes(
            local_connector=self.connector_id, local_emote=local_id, service=service
        )
        await self._reply(ctx, summary)

    async def _mirror_emote(
        self, ctx, local_id: str | None = None, service: str | None = None, new_name: str | None = None
    ) -> None:
        """`/mirror emote to <local_id|name> [<service>|all] [new_name]`."""
        if not await self._require_admin(ctx):
            return
        if not await self._linker_configured(ctx, self._emote_linker, "Linking isn't configured."):
            return
        if not local_id:
            await self._reply(ctx, "Which emote? Pass an emoji id or name.")
            return
        if service is None or service.lower() == "all":
            coro = self._emote_linker.mirror_emote_all(local_connector=self.connector_id, local_emote=local_id)
        else:
            coro = self._emote_linker.mirror_emote(
                local_connector=self.connector_id, local_emote=local_id, destination=service, new_name=new_name
            )
        await self._reply_linker_result(ctx, coro, log_context="/mirror emote")

    async def _mirror_emote_from(
        self, ctx, service: str, external_id: str, new_name: str | None = None
    ) -> None:
        """`/mirror emote from <service> <external_id|name> [new_name]`:
        recreate-or-match `service`'s custom emoji locally and link them."""
        if not await self._require_admin(ctx):
            return
        if not await self._linker_configured(ctx, self._emote_linker, "Linking isn't configured."):
            return
        await self._reply_linker_result(
            ctx,
            self._emote_linker.mirror_emote_from(
                local_connector=self.connector_id, source=service, source_emote=external_id, new_name=new_name
            ),
            log_context="/mirror emote from",
        )

    async def _link_user(self, ctx, service: str, external_id: str, local_id: str) -> None:
        if not await self._require_admin(ctx):
            return
        if not await self._linker_configured(ctx, self._user_linker, "User linking isn't configured."):
            return
        logger.info(
            "[stoat:%s] %s ran /link user service=%s external_id=%s local_id=%s",
            self.connector_id,
            ctx.author_id,
            service,
            external_id,
            local_id,
        )
        await self._reply_linker_result(
            ctx,
            self._user_linker.link_user(
                local_connector=self.connector_id,
                local_user_id=local_id,
                source=service,
                source_user_id=external_id,
            ),
            log_context="/link user",
        )

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
        if not await self._require_admin(ctx):
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

        if not await self._linker_configured(ctx, self._linker, "Linking isn't configured."):
            return
        channel_category = await self.get_channel_category_name(channel_id)
        logger.info(
            "[stoat:%s] %s ran /mirror channel local_id=%s service=%s",
            self.connector_id,
            ctx.author_id,
            channel_id,
            service,
        )
        if service is None or service.lower() == "all":
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
            )
        await self._reply_linker_result(ctx, coro, log_context="/mirror channel")

    async def _mirror_channel_from(
        self, ctx, service: str, external_id: str, new_name: str | None = None, category: str | None = None
    ) -> None:
        """`/mirror channel from <service> <external_id|name> [new_name] [category:<id|name>]`:
        create a local channel mirroring `service`'s and link them, landing it in
        the local counterpart of the source channel's linked Category - or in
        `category` (a local Category id/name), if given, which overrides that
        (issue #75)."""
        if not await self._require_admin(ctx):
            return
        if not await self._linker_configured(ctx, self._linker, "Linking isn't configured."):
            return
        await self._reply_linker_result(
            ctx,
            self._linker.mirror_channel_from(
                local_connector=self.connector_id,
                source=service,
                source_id=external_id,
                new_name=new_name,
                local_category=category,
            ),
            log_context="/mirror channel from",
        )

    async def _unlink_channel(self, ctx, local_id: str | None = None, service: str | None = None) -> None:
        """`/unlink channel [local_id|name] [<service>|all]`: local_id
        defaults to the invoking channel; service defaults to "all"
        (dissolving the whole bridge group)."""
        if not await self._require_admin(ctx):
            return
        channel_id = local_id if local_id else str(ctx.channel.id)

        if not await self._linker_configured(ctx, self._linker, "Linking isn't configured."):
            return
        logger.info(
            "[stoat:%s] %s ran /unlink channel local_id=%s service=%s",
            self.connector_id,
            ctx.author_id,
            channel_id,
            service,
        )
        await self._reply_linker_result(
            ctx,
            self._linker.unlink_channel(local_connector=self.connector_id, local_channel_id=channel_id, destination=service),
            log_context="/unlink channel",
        )

    async def _unlink_category(self, ctx, local_id: str | None = None, service: str | None = None) -> None:
        """`/unlink category [<local_id|name>] [<service>|all]`: local Category
        defaults to the invoking channel's own; service defaults to "all"
        (dissolving the whole bridge group)."""
        if not await self._require_admin(ctx):
            return
        if not await self._linker_configured(ctx, self._category_linker, "Category linking isn't configured."):
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
        await self._reply_linker_result(
            ctx,
            self._category_linker.unlink_category(
                local_connector=self.connector_id,
                local_category_id=str(category.id) if category is not None else None,
                local_category=local_id,
                destination=service,
            ),
            log_context="/unlink category",
        )

    async def _mirror_category(
        self, ctx, local_id: str | None = None, service: str | None = None, new_name: str | None = None
    ) -> None:
        """`/mirror category [<local_id|name>] [<service>|all] [new_name]`."""
        if not await self._require_admin(ctx):
            return
        if not await self._linker_configured(ctx, self._category_linker, "Category linking isn't configured."):
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
        if service is None or service.lower() == "all":
            coro = self._category_linker.mirror_category_all(**kwargs)
        else:
            coro = self._category_linker.mirror_category(destination=service, new_name=new_name, **kwargs)
        await self._reply_linker_result(
            ctx, coro, log_context="/mirror category", empty_fallback="Nothing to mirror."
        )

    async def _mirror_category_from(
        self, ctx, service: str, external_id: str, new_name: str | None = None
    ) -> None:
        """`/mirror category from <service> <external_id|name> [new_name]`: create
        a local Category mirroring `service`'s, link them, and relocate/mirror
        its channels into the local Category."""
        if not await self._require_admin(ctx):
            return
        if not await self._linker_configured(ctx, self._category_linker, "Category linking isn't configured."):
            return
        await self._reply_linker_result(
            ctx,
            self._category_linker.mirror_category_from(
                local_connector=self.connector_id, source=service, source_id=external_id, new_name=new_name
            ),
            log_context="/mirror category from",
            empty_fallback="Nothing to mirror.",
        )

    async def _unlink_user(self, ctx, service: str | None = None, local_id: str | None = None) -> None:
        """`/unlink user [service|all] [local_id|name]`: service defaults
        to "all" (dissolving the whole link group); local_id defaults to the
        invoking user themselves."""
        if not await self._require_admin(ctx):
            return
        target = local_id if local_id else str(ctx.author_id)

        if not await self._linker_configured(ctx, self._user_linker, "User linking isn't configured."):
            return
        logger.info(
            "[stoat:%s] %s ran /unlink user service=%s local_id=%s",
            self.connector_id,
            ctx.author_id,
            service,
            target,
        )
        await self._reply_linker_result(
            ctx,
            self._user_linker.unlink_user(local_connector=self.connector_id, local_user_id=target, destination=service),
            log_context="/unlink user",
        )

    async def _link_role(self, ctx, local_id: str, service: str, external_id: str) -> None:
        """`/link role <local_id|name> <service> <external_id|name>`."""
        if not await self._require_admin(ctx):
            return
        if not await self._linker_configured(ctx, self._role_linker, "Role linking isn't configured."):
            return
        logger.info(
            "[stoat:%s] %s ran /link role local=%s service=%s external=%s",
            self.connector_id,
            ctx.author_id,
            local_id,
            service,
            external_id,
        )
        await self._reply_linker_result(
            ctx,
            self._role_linker.link_role(
                local_connector=self.connector_id,
                local_role=local_id,
                source=service,
                source_role=external_id,
            ),
            log_context="/link role",
        )

    async def _unlink_role(self, ctx, local_id: str, service: str | None = None) -> None:
        """`/unlink role <local_id|name> [<service>|all]`."""
        if not await self._require_admin(ctx):
            return
        if not await self._linker_configured(ctx, self._role_linker, "Role linking isn't configured."):
            return
        await self._reply_linker_result(
            ctx,
            self._role_linker.unlink_role(local_connector=self.connector_id, local_role=local_id, destination=service),
            log_context="/unlink role",
        )

    async def _linked_roles(self, ctx, local_id: str | None = None, service: str | None = None) -> None:
        """`/linked roles [<local_id|name>] [<service>|all]` - read-only."""
        if not await self._linker_configured(ctx, self._role_linker, "Role linking isn't configured."):
            return
        summary = await self._role_linker.list_linked_roles(
            local_connector=self.connector_id, local_role=local_id, service=service
        )
        await self._reply(ctx, summary)

    async def _mirror_role(
        self, ctx, local_id: str | None = None, service: str | None = None, new_name: str | None = None
    ) -> None:
        """`/mirror role to <local_id|name> [<service>|all] [new_name]`."""
        if not await self._require_admin(ctx):
            return
        if not await self._linker_configured(ctx, self._role_linker, "Role linking isn't configured."):
            return
        if not local_id:
            await self._reply(ctx, "Which role? Pass a role id or name.")
            return
        if service is None or service.lower() == "all":
            coro = self._role_linker.mirror_role_all(local_connector=self.connector_id, local_role=local_id)
        else:
            coro = self._role_linker.mirror_role(
                local_connector=self.connector_id, local_role=local_id, destination=service, new_name=new_name
            )
        await self._reply_linker_result(ctx, coro, log_context="/mirror role")

    async def _mirror_role_from(
        self, ctx, service: str, external_id: str, new_name: str | None = None
    ) -> None:
        """`/mirror role from <service> <external_id|name> [new_name]`:
        create-or-match a local role mirroring `service`'s and link them."""
        if not await self._require_admin(ctx):
            return
        if not await self._linker_configured(ctx, self._role_linker, "Role linking isn't configured."):
            return
        await self._reply_linker_result(
            ctx,
            self._role_linker.mirror_role_from(
                local_connector=self.connector_id, source=service, source_role=external_id, new_name=new_name
            ),
            log_context="/mirror role from",
        )

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
