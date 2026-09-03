"""`StoatSenderService`: connection setup/teardown and inbound message relay.

Composes the command-handler (`StoatLinkingMixin`), resource-lookup
(`StoatLookupsMixin`) and sync-event (`StoatSyncMixin`) halves around the
lifecycle core: client construction, `start` / `close`, `on_ready`, and the
`_handle_message` relay path (including pin system-event handling and the
command-message de-dupe).

Instantiated once per configured Stoat connector (config.yaml's `stoat`
list can have any number of entries - public, self-hosted, or more).
"""

from __future__ import annotations

import logging
from collections import deque

import stoat

from stoat_discord_bridge.admin_commands import (
    CategoryLinker,
    ChannelLinker,
    EmoteLinker,
    RoleLinker,
    UserLinker,
)
from stoat_discord_bridge.config import StoatConnectorConfig
from stoat_discord_bridge.models import StandardMessage, StandardPin
from stoat_discord_bridge.services.base import (
    OnChannelRolePermissionChanged,
    OnEmojiCreated,
    OnEmojiDeleted,
    OnMemberRolesChanged,
    OnMessage,
    OnPin,
    OnReaction,
    OnRoleDeleted,
    OnRoleRenamed,
    OnTyping,
    SenderService,
)
from stoat_discord_bridge.services.stoat_service.client import _StoatClient
from stoat_discord_bridge.services.stoat_service.formatting import _avatar_url, _display_name, _map_attachments
from stoat_discord_bridge.services.stoat_service.linking import StoatLinkingMixin
from stoat_discord_bridge.services.stoat_service.lookups import StoatLookupsMixin
from stoat_discord_bridge.services.stoat_service.sync import StoatSyncMixin
from stoat_discord_bridge.status import HealthTracker

logger = logging.getLogger(__name__)


class StoatSenderService(StoatLinkingMixin, StoatLookupsMixin, StoatSyncMixin, SenderService):
    def __init__(
        self,
        config: StoatConnectorConfig,
        on_message: OnMessage,
        health: HealthTracker,
        on_reaction: OnReaction | None = None,
        on_emoji_created: OnEmojiCreated | None = None,
        on_emoji_deleted: OnEmojiDeleted | None = None,
        on_pin: OnPin | None = None,
        on_typing: OnTyping | None = None,
        linker: ChannelLinker | None = None,
        emote_linker: "EmoteLinker | None" = None,
        user_linker: "UserLinker | None" = None,
        category_linker: "CategoryLinker | None" = None,
        role_linker: "RoleLinker | None" = None,
        on_member_roles_changed: "OnMemberRolesChanged | None" = None,
        on_role_renamed: "OnRoleRenamed | None" = None,
        on_role_deleted: "OnRoleDeleted | None" = None,
        on_channel_role_permission_changed: "OnChannelRolePermissionChanged | None" = None,
    ) -> None:
        # linker/emote_linker/user_linker/category_linker/role_linker are only
        # needed to serve the corresponding `/link-*` / `/link ...` commands;
        # None is accepted (e.g. for tests) but those commands will then
        # report themselves unconfigured.
        SenderService.__init__(
            self, on_message, on_reaction, on_emoji_created, on_emoji_deleted, on_pin, on_typing
        )
        self._config = config
        self.server_id = config.server_id
        self.connector_id = config.id
        self._health = health
        self._linker = linker
        self._emote_linker = emote_linker
        self._user_linker = user_linker
        self._category_linker = category_linker
        self._role_linker = role_linker
        self._on_member_roles_changed = on_member_roles_changed
        self._on_role_renamed = on_role_renamed
        self._on_role_deleted = on_role_deleted
        self._on_channel_role_permission_changed = on_channel_role_permission_changed
        self._client = _StoatClient(self, config)
        self._self_id: str | None = None
        # ids of messages the ext.commands processor recognised as a bridge
        # command (`/link channel …`, `/status`, …) plus the bot's own command
        # replies - `_handle_message` drops these instead of relaying them.
        self._command_message_ids: deque[str] = deque(maxlen=512)

    async def _handle_ready(self, event) -> None:
        self._health.mark_connected(self.connector_id)
        self._self_id = str(event.me.id)
        logger.info("[stoat:%s] logged in as %s", self.connector_id, event.me.tag)

    # stoat.Client has no disconnect/logout-on-drop event to hook (only
    # on_before_connect/on_after_connect for the connect side, and on_logout
    # for an explicit logout) — so connected state here only ever turns on,
    # not off. A dropped connection still shows up as degraded/failing via
    # relay-error tracking in `receive()`.

    def _note_command_message(self, message_id: str) -> None:
        """Record `message_id` as a bridge-command message (the invoking
        `/…` or a bot command reply) so `_handle_message` won't relay it.
        Called from `_StoatClient.process_commands` and from `_reply`."""
        if message_id:
            self._command_message_ids.append(message_id)

    async def _reply(self, ctx, text: str) -> None:
        """Send a command's response. The single seam every `/…` handler uses
        instead of `ctx.send` directly - also flags the reply's id so the
        relay path in `_handle_message` drops it (belt-and-suspenders on top
        of the bot-author check, which already excludes it)."""
        sent = await ctx.send(text)
        self._note_command_message(str(getattr(sent, "id", "")))

    async def _handle_message(self, message) -> None:
        if getattr(message.author, "bot", False):
            return
        if str(message.id) in self._command_message_ids:
            # A `/link channel …` etc. already handled by the ext.commands
            # processor (which shares this MessageCreateEvent) - don't also
            # relay it as chat.
            return
        # A pin/unpin produces a system message (message_pinned /
        # message_unpinned) delivered here like any other. Turn it into a
        # StandardPin and don't relay it (otherwise it goes out as a blank
        # message). `.pinned_message_id` / `.unpinned_message_id` confirmed
        # against the installed stoat.py; the rest of the Stoat integration
        # is still assumed-against-a-live-server (see this module's TODOs).
        system_event = getattr(message, "system_event", None)
        if isinstance(system_event, (stoat.MessagePinnedSystemEvent, stoat.MessageUnpinnedSystemEvent)):
            pinned = isinstance(system_event, stoat.MessagePinnedSystemEvent)
            target_id = (
                system_event.pinned_message_id if pinned else system_event.unpinned_message_id
            )
            if self._on_pin is not None:
                await self._on_pin(
                    StandardPin(
                        origin_connector_id=self.connector_id,
                        origin_channel_id=str(message.channel.id),
                        origin_message_id=str(target_id),
                        pinned=pinned,
                    )
                )
            return
        # Bridge commands (`/link channel …`, `/status`, …) are handled by the
        # `stoat.ext.commands` processor on `_StoatClient` off this same event;
        # anything it recognised was already filtered out above by id.
        logger.debug(
            "[stoat:%s] message %s in channel %s from %s",
            self.connector_id,
            message.id,
            message.channel.id,
            message.author.id,
        )
        await self._on_message(
            StandardMessage(
                origin_connector_id=self.connector_id,
                origin_channel_id=str(message.channel.id),
                channel_name=getattr(message.channel, "name", str(message.channel.id)),
                sender_name=await self._resolve_sender_name(message),
                sender_avatar_url=await self._resolve_avatar_url(message),
                sender_user_id=str(message.author.id),
                content_markdown=message.content,
                message_id=str(message.id),
                attachments=_map_attachments(message),
            )
        )

    async def _resolve_sender_name(self, message) -> str:
        """Resolve `message.author`'s display name, fetching a fresh
        Member/User from the API when the cached copy on the message hasn't
        got one populated yet.

        Same cache-miss gap `_resolve_avatar_url` below already accounts
        for, just for the name rather than the avatar: a Member's underlying
        User isn't always cached by the time its message arrives, so
        `_display_name` can read the name as unset (stoat.py's
        Member.name/display_name silently return ""/None rather than the
        real value in that case - see get_masquerade_identity's docstring
        above) even though the sender has a real one set - relaying every
        message with a blank sender name until the cache happens to catch up
        on its own. Await the real lookup instead, same as the avatar: only
        taken when the cache is already known to be missing the name.
        """
        author = message.author
        name = _display_name(author)
        if name:
            return name
        try:
            server_id = getattr(message.channel, "server_id", None)
            if server_id is not None:
                fresh = await self._client.get_server(server_id, partial=True).fetch_member(author.id)
            else:
                fresh = await self._client.fetch_user(author.id)
        except Exception:
            return name
        return _display_name(fresh) or name

    async def _resolve_avatar_url(self, message) -> str | None:
        """Resolve `message.author`'s avatar, fetching a fresh Member/User
        from the API when the cached copy on the message hasn't got its
        avatar populated yet.

        A Member's underlying User isn't always cached by the time its
        message arrives (e.g. the bot hasn't chunked that member yet), so
        `_avatar_url` reads the avatar as unset even when the sender has a
        real one set, and falls back to the platform default - relaying
        every message with the wrong avatar until the cache happens to
        catch up on its own. Relaying isn't latency-sensitive enough for
        that to be worth it, so await the real lookup instead: only taken
        when the cache is already known to be missing the avatar.
        """
        author = message.author
        if getattr(author, "server_avatar", None) or getattr(author, "avatar", None):
            return _avatar_url(author)
        try:
            server_id = getattr(message.channel, "server_id", None)
            if server_id is not None:
                fresh = await self._client.get_server(server_id, partial=True).fetch_member(author.id)
            else:
                fresh = await self._client.fetch_user(author.id)
        except Exception:
            return _avatar_url(author)
        return _avatar_url(fresh)

    @property
    def self_id(self) -> str | None:
        """The bridge bot's own Stoat user id (set once on_ready fires),
        exposed for the receiver's own-reaction idempotency check."""
        return self._self_id

    async def start(self) -> None:
        # Credentials (token, http_base) are set at construction time above;
        # stoat.Client.start() takes no arguments (unlike discord.Client.start()).
        await self._client.start()

    async def close(self) -> None:
        await self._client.close()
