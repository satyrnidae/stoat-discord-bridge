"""The stoat.py client subclass for the Stoat connector.

stoat.py dispatches events by looking up `on_<event>` attributes on the
Client instance itself, so *something* has to subclass it. `_StoatClient`
exists only to satisfy that and delegates every callback to the owning
`StoatSenderService`. It subclasses `stoat.ext.commands.Bot` (rather than a
bare `stoat.Client`) so the admin commands are real prefix-command groups -
see `commands.build_command_tree`.
"""

from __future__ import annotations

import logging
from inspect import isawaitable
from typing import TYPE_CHECKING

from stoat.ext import commands as stoat_commands

from stoat_discord_bridge.config import StoatConnectorConfig
from stoat_discord_bridge.services.stoat_service.commands import build_command_tree
from stoat_discord_bridge.services.stoat_service.discovery import (
    _discover_cdn_base,
    _discover_node_config,
    _discover_websocket_base,
)

if TYPE_CHECKING:
    from stoat_discord_bridge.services.stoat_service.sender import StoatSenderService

logger = logging.getLogger(__name__)


class _StoatClient(stoat_commands.Bot):
    """stoat.py dispatches events by looking up `on_<event>` attributes on the
    Client instance itself, so *something* has to subclass stoat.Client. This
    subclass exists only to satisfy that and delegates every callback to the
    owning StoatSenderService, which otherwise doesn't need to inherit from a
    third-party client class.

    Subclasses `stoat.ext.commands.Bot` (a `stoat.Client` that also runs the
    prefix-command framework) rather than a bare `stoat.Client`, so the admin
    commands (`/link channel …` etc.) are real `commands.Group` subcommands -
    the Stoat analogue of the Discord side's `app_commands` groups - instead of
    a hand-rolled string-match ladder in `_handle_message`. `MessageCreateEvent`
    still also drives `on_message` (via `call_object_handlers_hook`), which is
    where message relay happens; `process_commands` below records the ids of
    messages it recognized as commands so `_handle_message` can skip relaying
    them."""

    def __init__(self, owner: "StoatSenderService", config: StoatConnectorConfig) -> None:
        node_config = _discover_node_config(config.api_url, connector_id=config.id)
        self._prefix = config.command_prefix
        super().__init__(
            config.command_prefix,
            token=config.bot_token,
            http_base=config.api_url,
            websocket_base=_discover_websocket_base(node_config),
            cdn_base=_discover_cdn_base(node_config),
        )
        self._owner = owner
        self._register_commands()

    async def on_ready(self, event, /) -> None:
        await self._owner._handle_ready(event)

    async def on_message(self, message, /) -> None:
        await self._owner._handle_message(message)

    async def on_message_update(self, event, /) -> None:
        # stoat.events.MessageUpdateEvent (event_name 'message_update'):
        # `.message` (PartialMessage, changed fields), `.before` / `.after`
        # (Optional[Message], cache-dependent). Verified against stoat.py
        # 1.2.1; live-server payload completeness unverified.
        await self._owner._handle_message_update(event)

    async def on_server_channel_create(self, event, /) -> None:
        await self._owner._handle_channel_create(event.channel)

    async def on_server_member_update(self, event, /) -> None:
        # stoat.events.ServerMemberUpdateEvent: `.member` (PartialMember,
        # the changed fields), `.before` / `.after` (Optional[Member], both
        # carrying `.id` / `.server_id` / `.role_ids`). `before` / `after` are
        # None when the member isn't cached. Verified against stoat.py 1.2.1
        # (event_name 'server_member_update' -> this handler); live-server
        # payload completeness still unverified.
        await self._owner._handle_member_update(event)

    async def on_raw_server_role_update(self, event, /) -> None:
        # stoat.events.RawServerRoleUpdateEvent (event_name
        # 'raw_server_role_update'): `.role` (PartialRole), `.old_role` /
        # `.new_role` (Optional[Role]), `.server` (Optional[Server]). Combined
        # create+update; `.old_role is None` => created *or* server uncached
        # (ignored here either way). Verified against stoat.py 1.2.1; live
        # server unverified.
        await self._owner._handle_role_update(event)

    async def on_server_role_delete(self, event, /) -> None:
        # stoat.events.ServerRoleDeleteEvent (event_name 'server_role_delete'):
        # `.server_id`, `.role_id`, plus Optional `.server` / `.role`. Verified
        # against stoat.py 1.2.1; live server unverified.
        await self._owner._handle_role_delete(event)

    async def on_channel_update(self, event, /) -> None:
        # stoat.events.ChannelUpdateEvent (event_name 'channel_update'):
        # `.channel` (PartialChannel, the changed fields), `.before` / `.after`
        # (Optional[Channel]). A server channel's `.role_permissions` is
        # `dict[str, PermissionOverride]` (each with `.allow` / `.deny`
        # Permissions); PartialChannel carries it too but only when it changed,
        # and the private-channel members of the Channel union lack it - hence
        # the getattr guards in _handle_channel_update. Verified against
        # stoat.py 1.2.1; live server unverified.
        await self._owner._handle_channel_update(event)

    async def on_channel_start_typing(self, event, /) -> None:
        # stoat.events.ChannelStartTypingEvent (.channel_id, .user_id).
        await self._owner._handle_typing(event, active=True)

    async def on_channel_stop_typing(self, event, /) -> None:
        # stoat.events.ChannelStopTypingEvent (.channel_id, .user_id).
        await self._owner._handle_typing(event, active=False)

    async def on_message_react(self, event, /) -> None:
        await self._owner._handle_message_react(event, added=True)

    async def on_message_unreact(self, event, /) -> None:
        await self._owner._handle_message_react(event, added=False)

    async def on_server_emoji_create(self, event, /) -> None:
        await self._owner._handle_emoji_create(event.emoji)

    async def on_server_emoji_delete(self, event, /) -> None:
        await self._owner._handle_emoji_delete(getattr(event, "emoji_id", None) or event.emoji.id)

    # ----------------------------------------------------------- commands

    async def process_commands(self, message, shard, /) -> None:
        """Same as `commands.Bot.process_commands`, but records the id of any
        message that resolved to one of our registered commands so
        `_handle_message` (driven independently off the same MessageCreateEvent
        via `call_object_handlers_hook`) knows not to also relay it as chat.
        The record is taken even when argument parsing later fails, so a
        malformed `/link channel` still isn't relayed."""
        ctx = await self.get_context(message, shard)
        if ctx.command is not None:
            self._owner._note_command_message(str(message.id))
        skip = self.skip_check(ctx)
        if isawaitable(skip):
            skip = await skip
        if skip:
            return
        await self.invoke(ctx)

    async def on_command_error(self, event, /) -> None:
        error = event.error
        ctx = event.context
        if isinstance(error, stoat_commands.CommandNotFound):
            return
        if isinstance(error, stoat_commands.UserInputError):
            if ctx.command is not None:
                sig = ctx.command.signature
                usage = f"Usage: {self._prefix}{ctx.command.qualified_name}" + (f" {sig}" if sig else "")
            else:
                usage = "Bad command usage."
            await self._owner._reply(ctx, usage)
            return
        logger.error(
            "[stoat:%s] command %r failed",
            self._owner.connector_id,
            getattr(ctx.command, "qualified_name", "?"),
            exc_info=error,
        )
        await self._owner._reply(ctx, "That command failed.")

    def _register_commands(self) -> None:
        build_command_tree(self, self._owner, self._prefix)
