"""`DiscordSenderService`: connection setup/teardown and inbound message relay.

Composes the command-handler (`DiscordLinkingMixin`), resource-lookup
(`DiscordLookupsMixin`) and sync-event (`DiscordSyncMixin`) halves around
the lifecycle core: client + command-tree construction, `start` / `close`,
`on_ready` (which syncs the slash commands), and the `_handle_message` /
`_handle_thread_create` relay path.

Instantiated once per configured Discord connector (config.yaml's `discord`
list can have any number of entries) since each guild needs its own
discord.Client/command tree.
"""

from __future__ import annotations

import logging
from dataclasses import replace

import discord

from stoat_discord_bridge.admin_commands import (
    CategoryLinker,
    ChannelLinker,
    EmoteLinker,
    RoleLinker,
    UserLinker,
)
from stoat_discord_bridge.channel_structure import clip_name
from stoat_discord_bridge.config import DiscordConnectorConfig
from stoat_discord_bridge.models import StandardMessage, StandardPin, StandardTyping
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
from stoat_discord_bridge.services.caching import AsyncTTLCache
from stoat_discord_bridge.services.discord_service.client import _DiscordClient
from stoat_discord_bridge.services.discord_service.commands import build_command_tree
from stoat_discord_bridge.services.discord_service.formatting import _to_standard_message
from stoat_discord_bridge.services.discord_service.linking import DiscordLinkingMixin
from stoat_discord_bridge.services.discord_service.lookups import DiscordLookupsMixin
from stoat_discord_bridge.services.discord_service.sync import DiscordSyncMixin
from stoat_discord_bridge.status import HealthTracker

logger = logging.getLogger(__name__)

# How long a resolved (or absent) pronoun value is cached per user before the
# profile endpoint is consulted again - long enough to keep a busy channel
# from hammering it, short enough that a pronoun set later shows up soon.
_PRONOUN_CACHE_TTL = 600.0


class DiscordSenderService(DiscordLinkingMixin, DiscordLookupsMixin, DiscordSyncMixin, SenderService):
    def __init__(
        self,
        config: DiscordConnectorConfig,
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
        # needed to serve the corresponding `/link-*` commands; None is
        # accepted (e.g. for tests) but those commands will then report
        # themselves unconfigured.
        SenderService.__init__(
            self, on_message, on_reaction, on_emoji_created, on_emoji_deleted, on_pin, on_typing
        )
        self._config = config
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
        self._commands_synced = False
        # Discord thread auto-mirror (_handle_thread_create) bookkeeping - see
        # both methods' docstrings. _pending_thread_starter maps a thread id
        # whose mirror is in flight to its buffered starter message (None until
        # _handle_message sees it); _thread_ready holds thread ids whose mirror
        # finished before the starter message arrived, so the next in-thread
        # message relays normally instead of being buffered.
        self._pending_thread_starter: dict[int, "discord.Message | None"] = {}
        self._thread_ready: set[int] = set()
        # user id -> pronouns (or None), so the profile endpoint isn't hit on
        # every relayed message - see `_resolve_sender_pronouns`.
        self._pronoun_cache: AsyncTTLCache[str | None] = AsyncTTLCache(_PRONOUN_CACHE_TTL)
        self._guild = discord.Object(id=config.guild_id)
        self._client = _DiscordClient(self)
        self.tree = discord.app_commands.CommandTree(self._client)
        build_command_tree(self)

    @property
    def client(self) -> discord.Client:
        return self._client

    async def _handle_ready(self) -> None:
        self._health.mark_connected(self.connector_id)
        if not self._commands_synced:
            try:
                synced = await self.tree.sync(guild=self._guild)
            except Exception:
                logger.exception(
                    "[discord:%s] slash command sync failed; Discord still has the "
                    "previous command set - will retry on next ready",
                    self.connector_id,
                )
            else:
                self._commands_synced = True
                logger.info(
                    "[discord:%s] synced %d slash command(s): %s",
                    self.connector_id,
                    len(synced),
                    ", ".join(sorted(c.name for c in synced)) or "(none)",
                )
        logger.info(
            "[discord:%s] logged in as %s (guild %s)", self.connector_id, self._client.user, self._config.guild_id
        )

    async def _handle_disconnect(self) -> None:
        self._health.mark_disconnected(self.connector_id)
        logger.warning("[discord:%s] disconnected", self.connector_id)

    async def _handle_message(self, message: discord.Message) -> None:
        if message.author.bot:
            return
        if message.guild is None or message.guild.id != self._config.guild_id:
            return
        if message.type is discord.MessageType.pins_add:
            # Discord's own "<user> pinned a message to this channel" system
            # message - suppressed here so it isn't relayed as a blank
            # message. The pin itself is synced via _handle_raw_message_edit.
            return
        if message.type is discord.MessageType.thread_created:
            # Discord's own "<user> started a thread" system message in the
            # parent channel - suppressed here; _handle_thread_create posts the
            # bot notice itself, once the thread's mirror channel exists and is
            # linked so the <#thread> mention can resolve to it.
            return
        if message.channel.id in self._pending_thread_starter:
            # The starter message of a thread _handle_thread_create is still
            # mirroring - buffer it so that handler can relay it (as this
            # user) once the destination channel actually exists and is linked.
            self._pending_thread_starter[message.channel.id] = message
            return
        # If the mirror finished before the starter arrived, _thread_ready
        # holds the thread id; drop it and relay this message normally.
        self._thread_ready.discard(message.channel.id)
        logger.debug(
            "[discord:%s] message %s in channel %s from %s",
            self.connector_id,
            message.id,
            message.channel.id,
            message.author.id,
        )
        await self._on_message(
            _to_standard_message(
                message,
                self.connector_id,
                source_label=self._config.label,
                sender_pronouns=await self._resolve_sender_pronouns(message.author.id),
            )
        )

    async def _resolve_sender_pronouns(self, user_id: int | str) -> str | None:
        """Best-effort pronouns for `user_id`, cached per user
        (`_pronoun_cache`). discord.py 2.7.1 has no pronoun API, so this hits
        the (undocumented) `GET /users/{id}/profile` REST endpoint by hand -
        `guild_member_profile.pronouns` (this guild's per-server value) is
        preferred over `user_profile.pronouns` (the account-wide one). Any
        failure - the endpoint 404ing, rate-limiting, changing shape, the
        connector's `pronoun_forwarding` being off - just yields None and the
        message relays without pronouns."""
        if not self._config.pronoun_forwarding:
            return None
        return await self._pronoun_cache.get(str(user_id), self._fetch_pronouns_from_profile)

    async def _fetch_pronouns_from_profile(self, user_id: str) -> str | None:
        try:
            data = await self._client.http.request(
                discord.http.Route("GET", "/users/{user_id}/profile", user_id=user_id),
                params={"guild_id": str(self._config.guild_id), "with_mutual_guilds": "false"},
            )
        except Exception:  # noqa: BLE001 - best-effort; any failure just means "no pronouns"
            logger.debug("[discord:%s] couldn't fetch profile for %s", self.connector_id, user_id, exc_info=True)
            return None
        for section in ("guild_member_profile", "user_profile"):
            pronouns = (data.get(section) or {}).get("pronouns") if isinstance(data, dict) else None
            if pronouns:
                return str(pronouns).strip() or None
        return None

    async def _handle_raw_message_edit(self, payload: discord.RawMessageUpdateEvent) -> None:
        """MESSAGE_UPDATE fires both when a message is pinned and when it's
        unpinned (Discord has a `pins_add` system message but no `pins_remove`
        one, so this is the only event that covers both directions). Emit a
        StandardPin whenever the payload carries a `pinned` field.

        Heuristic: a pin toggle sends a minimal payload
        (`{id, channel_id, guild_id, pinned}`) while a content edit carries
        `content`/`edited_timestamp` and no `pinned` - so `"pinned" in data`
        distinguishes them. A stray resync (e.g. a future payload shape that
        includes `pinned` on every edit) is harmless: the receiver's
        set_pinned() is idempotent and BridgeCoordinator suppresses the echo.
        """
        if self._on_pin is None or payload.guild_id != self._config.guild_id:
            return
        data = payload.data or {}
        if "pinned" not in data:
            return
        await self._on_pin(
            StandardPin(
                origin_connector_id=self.connector_id,
                origin_channel_id=str(payload.channel_id),
                origin_message_id=str(payload.message_id),
                pinned=bool(data["pinned"]),
            )
        )

    async def _handle_typing(self, channel, user) -> None:
        """`on_typing`: a user started typing in a channel. Relay it across the
        bridge (BridgeCoordinator scopes it to a mapped channel). Dropped for
        DMs, other guilds, and the bridge bot's own typing (which its own
        `trigger_typing` on the receiver side would otherwise echo back)."""
        if self._on_typing is None:
            return
        guild = getattr(channel, "guild", None)
        if guild is None or guild.id != self._config.guild_id:
            return
        self_user = self._client.user
        if getattr(user, "bot", False) or (self_user is not None and user.id == self_user.id):
            return
        await self._on_typing(
            StandardTyping(
                origin_connector_id=self.connector_id,
                origin_channel_id=str(channel.id),
                sender_name=getattr(user, "display_name", None) or getattr(user, "name", str(user.id)),
                sender_user_id=str(user.id),
            )
        )

    async def _handle_thread_create(self, thread: discord.Thread) -> None:
        """A Discord thread (including a forum post, also a discord.Thread -
        see _get_or_create_webhook's docstring) has no IRC/Stoat equivalent,
        so instead of relaying its starter message as plain text, bundle a
        `/mirror channel all`-style request: ensure a same-named channel
        exists and is linked on every other connector, then relay the
        thread's own starter message into it as the originating user. Only
        fires for a thread whose parent channel is itself already bridged,
        so this doesn't auto-mirror every thread created anywhere in the
        guild. One-way (Discord -> Stoat/IRC) only; Stoat/IRC have no
        equivalent "created a thread" event of their own yet. The parent
        channel's own "<user> started a thread" system message is turned
        into a bot notice separately - see _relay_thread_created_notice.

        `thread.id` is recorded in _pending_thread_starter *before* any
        `await` below so _handle_message buffers the thread's starter
        message instead of relaying it into a channel that isn't linked
        yet: discord.py dispatches gateway events (and so schedules each
        handler's task) in the order they're received, and THREAD_CREATE
        always precedes the MESSAGE_CREATE for a new thread's first message.

        The mirrored channel is placed into a Category named after the
        thread's *parent channel* - not any real Discord Category the parent
        itself belongs to - so every thread/forum-post under the same parent
        groups together on the destination, deliberately overriding the
        general "mirror the source's own Category" rule /mirror channel
        otherwise follows. The Category takes each destination's *own* name
        for the parent channel (via `category_from_channel_id`), falling back
        to the Discord name only where the parent isn't linked there.
        """
        if self._linker is None or thread.guild.id != self._config.guild_id:
            return
        parent = thread.parent
        if parent is None or not await self._linker.is_linked(self.connector_id, str(parent.id)):
            return  # this thread's parent was never bridged - leave the thread alone

        self._pending_thread_starter[thread.id] = None
        try:
            result = await self._linker.mirror_channel_all(
                local_connector=self.connector_id,
                local_channel_id=str(thread.id),
                local_channel_name=clip_name(thread.name),
                local_channel_category=clip_name(parent.name),
                is_thread_category=True,
                category_from_channel_id=str(parent.id),
            )
        except Exception:
            logger.exception("[discord:%s] failed to auto-mirror thread %s", self.connector_id, thread.id)
            self._pending_thread_starter.pop(thread.id, None)
            self._thread_ready.discard(thread.id)
            return
        logger.info(
            "[discord:%s] auto-mirrored thread %s (%s): %s",
            self.connector_id,
            thread.id,
            thread.name,
            result.replace("\n", " | "),
        )

        starter = self._pending_thread_starter.pop(thread.id, None)
        if starter is None:
            try:  # thread created from an existing message: no fresh MESSAGE_CREATE fires
                starter = thread.starter_message or await thread.fetch_message(thread.id)
            except Exception:  # noqa: BLE001 - best-effort; fall back to _thread_ready below
                starter = None
        starter_author = getattr(starter, "author", None)
        if starter is not None and getattr(starter, "type", discord.MessageType.default) not in (
            discord.MessageType.default,
            discord.MessageType.reply,
        ):
            # A thread opened without a starting message (a standalone thread, or
            # a forum post's system row) has no real first message - fetch_message
            # hands back the "started this thread" system message, whose content
            # is just the thread name. Keep its author for the notice, but don't
            # relay the row itself as if the user typed it.
            starter = None

        if starter is None:
            # No starter message to relay yet - let _handle_message relay the
            # next in-thread message normally now that the channel exists.
            self._thread_ready.add(thread.id)
        else:
            msg = _to_standard_message(
                starter,
                self.connector_id,
                source_label=self._config.label,
                sender_pronouns=await self._resolve_sender_pronouns(starter.author.id),
            )
            await self._on_message(replace(msg, origin_channel_id=str(thread.id), channel_name=thread.name))

        await self._relay_thread_created_notice(thread, starter_author)

    async def _relay_thread_created_notice(self, thread: discord.Thread, starter_author: object) -> None:
        """Post a bot-authored "<user> started a thread: <#thread>" notice into
        the thread's parent channel, standing in for Discord's own system
        message (which _handle_message suppresses).

        Called only after the thread has been mirrored + linked, so each
        receiver's rewrite_channel_mentions can turn the `<#thread-id>` mention
        into its own linked copy of the mirrored channel (`#<thread name>` on
        IRC), falling back to `#<thread name>` only if it still can't resolve."""
        parent = thread.parent
        if parent is None:
            return
        bot_user = self._client.user
        who = await self._thread_starter_name(thread, starter_author)
        await self._on_message(
            StandardMessage(
                origin_connector_id=self.connector_id,
                origin_channel_id=str(parent.id),
                channel_name=getattr(parent, "name", str(parent.id)),
                sender_name=bot_user.display_name if bot_user is not None else "Bridge",
                sender_avatar_url=(
                    str(bot_user.display_avatar.url) if bot_user is not None and bot_user.display_avatar else None
                ),
                sender_user_id=str(bot_user.id) if bot_user is not None else "",
                content_markdown=f"{who} started a thread: <#{thread.id}>",
                message_id=f"thread-created:{thread.id}",
            )
        )

    async def _thread_starter_name(self, thread: discord.Thread, starter_author: object) -> str:
        """Best-effort display name of whoever opened the thread: the starter
        message's author if we have one, else the thread owner (from cache, then
        a fetch), else a neutral fallback."""
        name = getattr(starter_author, "display_name", None)
        if name:
            return name
        owner = getattr(thread, "owner", None)
        if owner is not None and getattr(owner, "display_name", None):
            return owner.display_name
        owner_id = getattr(thread, "owner_id", None)
        if owner_id:
            try:
                member = thread.guild.get_member(owner_id) or await thread.guild.fetch_member(owner_id)
                if member is not None:
                    return member.display_name
            except Exception:  # noqa: BLE001 - best-effort display name only
                pass
        return "Someone"

    async def start(self) -> None:
        await self._client.start(self._config.bot_token)

    async def close(self) -> None:
        await self._client.close()
