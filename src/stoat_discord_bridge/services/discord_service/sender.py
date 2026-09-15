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
    BotWhitelistManager,
    CategoryLinker,
    ChannelLinker,
    EmoteLinker,
    MirrorInProgressError,
    RoleLinker,
    UserLinker,
)
from stoat_discord_bridge.channel_structure import clip_name
from stoat_discord_bridge.config import DiscordConnectorConfig
from stoat_discord_bridge.models import StandardDelete, StandardEdit, StandardMessage, StandardPin, StandardTyping
from stoat_discord_bridge.services.base import (
    OnChannelRenamed,
    OnChannelRolePermissionChanged,
    OnDelete,
    OnEdit,
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
# from stoat_discord_bridge.services.caching import AsyncTTLCache  # noqa: ERA001 - re-enable with _resolve_sender_pronouns (issue #58)
from stoat_discord_bridge.services.discord_service.client import _DiscordClient
from stoat_discord_bridge.services.discord_service.commands import build_command_tree
from stoat_discord_bridge.services.discord_service.formatting import (
    _map_mentioned_channels,
    _map_mentioned_roles,
    _map_mentioned_users,
    _member_color,
    _to_standard_message,
)
from stoat_discord_bridge.services.discord_service.linking import DiscordLinkingMixin
from stoat_discord_bridge.services.discord_service.lookups import DiscordLookupsMixin
from stoat_discord_bridge.services.discord_service.sync import DiscordSyncMixin
from stoat_discord_bridge.services.mentions import _defang_mentions
from stoat_discord_bridge.services.voice.base import OnVoiceConnectorLost, OnVoicePresence
from stoat_discord_bridge.status import HealthTracker

logger = logging.getLogger(__name__)

# Discord pronoun resolution is disabled (issue #58): there is no
# bot-accessible pronoun source. Kept for a future re-enable - see
# `DiscordSenderService._resolve_sender_pronouns`.
# How long a resolved (or absent) pronoun value is cached per user before the
# profile endpoint is consulted again - long enough to keep a busy channel
# from hammering it, short enough that a pronoun set later shows up soon.
# _PRONOUN_CACHE_TTL = 600.0  # noqa: ERA001 - re-enable with _resolve_sender_pronouns (issue #58)


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
        on_edit: OnEdit | None = None,
        on_delete: OnDelete | None = None,
        on_voice_presence: "OnVoicePresence | None" = None,
        on_voice_connector_lost: "OnVoiceConnectorLost | None" = None,
        linker: ChannelLinker | None = None,
        emote_linker: "EmoteLinker | None" = None,
        user_linker: "UserLinker | None" = None,
        category_linker: "CategoryLinker | None" = None,
        role_linker: "RoleLinker | None" = None,
        bot_whitelist: "BotWhitelistManager | None" = None,
        on_member_roles_changed: "OnMemberRolesChanged | None" = None,
        on_role_renamed: "OnRoleRenamed | None" = None,
        on_role_deleted: "OnRoleDeleted | None" = None,
        on_channel_role_permission_changed: "OnChannelRolePermissionChanged | None" = None,
        on_channel_renamed: "OnChannelRenamed | None" = None,
    ) -> None:
        # linker/emote_linker/user_linker/category_linker/role_linker are only
        # needed to serve the corresponding `/link-*` commands; None is
        # accepted (e.g. for tests) but those commands will then report
        # themselves unconfigured.
        SenderService.__init__(
            self,
            on_message,
            on_reaction,
            on_emoji_created,
            on_emoji_deleted,
            on_pin,
            on_typing,
            on_edit,
            on_delete,
        )
        self._config = config
        self.connector_id = config.id
        self._health = health
        self._linker = linker
        self._emote_linker = emote_linker
        self._user_linker = user_linker
        self._category_linker = category_linker
        self._role_linker = role_linker
        self._bot_whitelist = bot_whitelist
        self._on_voice_presence = on_voice_presence
        self._on_voice_connector_lost = on_voice_connector_lost
        self._on_member_roles_changed = on_member_roles_changed
        self._on_role_renamed = on_role_renamed
        self._on_role_deleted = on_role_deleted
        self._on_channel_role_permission_changed = on_channel_role_permission_changed
        self._on_channel_renamed = on_channel_renamed
        self._commands_synced = False
        # Discord thread auto-mirror (_handle_thread_create) bookkeeping - see
        # both methods' docstrings. _pending_thread_starter maps a thread id
        # whose mirror is in flight to its buffered starter message (None until
        # _handle_message sees it); _thread_ready holds thread ids whose mirror
        # finished before the starter message arrived, so the next in-thread
        # message relays normally instead of being buffered.
        self._pending_thread_starter: dict[int, "discord.Message | None"] = {}
        self._thread_ready: set[int] = set()
        # Discord pronoun resolution is disabled (issue #58) - no bot-accessible
        # source exists. Restore this cache alongside `_resolve_sender_pronouns`
        # if that ever changes:
        # self._pronoun_cache: AsyncTTLCache[str | None] = AsyncTTLCache(_PRONOUN_CACHE_TTL)
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
        # Pull the full member roster into cache (after the command sync, which
        # matters more for availability) so the `/link` etc. slash commands'
        # user autocomplete (DiscordLookupsMixin.list_users, which must not do
        # I/O per keystroke) sees every member, not just the ones who happened
        # to speak while the bot was running (issue #80). Needs the privileged
        # members intent - already enabled. Best-effort and one-shot: the
        # `chunked` guard skips it on every later reconnect, and a failure just
        # leaves the roster as sparse as it was.
        guild = self._guild_or_none()
        if guild is not None and not guild.chunked:
            try:
                await guild.chunk()
            except Exception:
                logger.warning(
                    "[discord:%s] member chunk failed; user autocomplete will only "
                    "list members already cached from activity",
                    self.connector_id,
                    exc_info=True,
                )
        logger.info(
            "[discord:%s] logged in as %s (guild %s)", self.connector_id, self._client.user, self._config.guild_id
        )

    async def _handle_disconnect(self) -> None:
        self._health.mark_disconnected(self.connector_id)
        logger.warning("[discord:%s] disconnected", self.connector_id)
        # discord.py's on_disconnect also fires on a transient reconnect, not
        # only a permanent drop - VoiceBridgeCoordinator.connector_disconnected
        # (issue #113) is cheap and safe either way (a no-op unless this
        # connector was actually part of a live session). Its presence isn't
        # re-seeded until the coordinator's own periodic refresh (up to
        # _REFRESH_INTERVAL later) or the next live presence push on this
        # connector - _handle_ready doesn't trigger an immediate
        # refresh_groups() yet, so a fast reconnect can show this connector
        # as briefly empty rather than instantly restoring its occupants.
        if self._on_voice_connector_lost is not None:
            await self._on_voice_connector_lost(self.connector_id)

    async def _bot_is_whitelisted(self, user_id: str) -> bool:
        """Whether a bot-authored event from `user_id` should relay like a
        human's, per `BotWhitelistManager.is_whitelisted` (issue #120). False
        (today's behavior) when bot whitelisting isn't wired at all."""
        if self._bot_whitelist is None:
            return False
        return await self._bot_whitelist.is_whitelisted(self.connector_id, user_id)

    async def _handle_message(self, message: discord.Message) -> None:
        if message.webhook_id is not None:
            # Our own (or another integration's) webhook post - always
            # dropped, unconditionally, regardless of the bot whitelist
            # (issue #120). This is the actual loop guard; a webhook
            # message's author also reports `bot=True`, but that half is
            # relaxable per-bot below.
            return
        if message.author.bot and not await self._bot_is_whitelisted(str(message.author.id)):
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
        if message.type is discord.MessageType.channel_name_change and isinstance(message.channel, discord.Thread):
            # Discord's own "<user> changed the post title/channel name: <new
            # name>" system message in the thread/forum-post itself - the
            # displayed wording is client-rendered from the message type, not
            # stored content (.content is just the bare new name) - suppressed
            # here and replaced with a bot notice + rename sync (issue #152).
            await self._relay_thread_renamed_notice(message)
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
                # Discord pronoun resolution is disabled - see
                # `_resolve_sender_pronouns` (issue #58).
                sender_pronouns=await self._resolve_sender_pronouns(message.author.id),
                sender_color=self._resolve_sender_color(message.author),
            )
        )

    async def fetch_history(self, channel_id: str, limit: int | None) -> list[StandardMessage]:
        """`ConnectorInfo.fetch_history` for Discord (issue #122): `channel_id`'s
        history, converted via the same `_to_standard_message` the live relay
        path uses, always returned oldest-first so relaying it in list order
        reproduces the original chronology. `limit=None` fetches the entire
        channel history, oldest message first (archive mode's `limit:all`);
        a numeric `limit` instead fetches the `limit` *most recent* raw
        messages (discord.py's own newest-first `history()` default) and
        reverses them into oldest-first order - so a bounded backfill seeds
        the freshly-linked channel with recent context, not its oldest
        messages. A message this connector's own filtering below drops still
        counts against that raw `limit`, so a very noisy tail of skipped
        messages can leave fewer than `limit` converted ones - the same
        approximate-count tradeoff `ensure_channel`'s other best-effort hooks
        already make elsewhere in this file.

        Filters out what `_handle_message` would already drop from a live
        feed - a channel outside this connector's configured guild, our own
        (or another integration's) webhook posts, a non-whitelisted bot's
        messages, and non-content system messages (pin/thread-created rows) -
        so a backfill doesn't relay noise a live listener never would have.
        Best-effort: an unresolvable/wrong-guild channel yields no messages;
        a fetch that raises partway through the walk yields whatever it
        managed to convert before that rather than discarding a long
        backfill's progress over one bad message."""
        try:
            channel = self._client.get_channel(int(channel_id)) or await self._client.fetch_channel(int(channel_id))
        except Exception:
            logger.warning("[discord:%s] fetch_history: couldn't resolve channel %s", self.connector_id, channel_id)
            return []
        guild = getattr(channel, "guild", None)
        if guild is None or guild.id != self._config.guild_id:
            logger.warning(
                "[discord:%s] fetch_history: channel %s isn't in this connector's guild", self.connector_id, channel_id
            )
            return []
        messages: list[StandardMessage] = []
        try:
            async for message in channel.history(limit=limit, oldest_first=limit is None):
                if message.webhook_id is not None:
                    continue
                if message.author.bot and not await self._bot_is_whitelisted(str(message.author.id)):
                    continue
                if message.type not in (discord.MessageType.default, discord.MessageType.reply):
                    continue
                messages.append(
                    _to_standard_message(
                        message,
                        self.connector_id,
                        source_label=self._config.label,
                        sender_pronouns=await self._resolve_sender_pronouns(message.author.id),
                        sender_color=self._resolve_sender_color(message.author),
                    )
                )
        except Exception:
            logger.warning(
                "[discord:%s] fetch_history: fetching channel %s failed", self.connector_id, channel_id, exc_info=True
            )
        if limit is not None:
            # `oldest_first=False` (above) walked newest-first so `limit` caps
            # the *most recent* messages - reverse back into oldest-first
            # relay order.
            messages.reverse()
        return messages

    def _resolve_sender_color(self, author: object) -> str | None:
        """The sender's displayed name color, for a receiver that can tint a
        relayed name (Stoat's masquerade, issue #74). Network-free - reads the
        member's already-resolved top-role color. None when this connector's
        `color_forwarding` is off, or the sender has no color."""
        if not self._config.color_forwarding:
            return None
        return _member_color(author)

    async def _resolve_sender_pronouns(self, user_id: int | str) -> str | None:
        """Always ``None`` on Discord: there is no bot-accessible pronoun
        field. discord.py 2.7.1 models none, and the only source - the
        undocumented ``GET /users/{id}/profile`` REST endpoint - is hard
        blocked for bot tokens (``403 Forbidden``, error code ``20001``:
        "Bots cannot use this endpoint"), so every call failed unconditionally
        and just logged a traceback per relayed message (issue #58). The
        connector's inbound `pronoun_forwarding` still governs whether an
        *incoming* message's pronouns show in the webhook name.

        The profile-fetch implementation is preserved (commented out) below:
        if Discord ever exposes user pronouns to bots - via the library or an
        endpoint bots may call - drop this stub, restore `_pronoun_cache` /
        `_PRONOUN_CACHE_TTL` / the `AsyncTTLCache` import, and re-enable it.
        """
        return None

    # async def _resolve_sender_pronouns(self, user_id: int | str) -> str | None:
    #     """Best-effort pronouns for `user_id`, cached per user
    #     (`_pronoun_cache`). discord.py 2.7.1 has no pronoun API, so this hits
    #     the (undocumented) `GET /users/{id}/profile` REST endpoint by hand -
    #     `guild_member_profile.pronouns` (this guild's per-server value) is
    #     preferred over `user_profile.pronouns` (the account-wide one). Any
    #     failure - the endpoint 404ing, rate-limiting, changing shape, the
    #     connector's `pronoun_forwarding` being off - just yields None and the
    #     message relays without pronouns."""
    #     if not self._config.pronoun_forwarding:
    #         return None
    #     return await self._pronoun_cache.get(str(user_id), self._fetch_pronouns_from_profile)
    #
    # async def _fetch_pronouns_from_profile(self, user_id: str) -> str | None:
    #     try:
    #         data = await self._client.http.request(
    #             discord.http.Route("GET", "/users/{user_id}/profile", user_id=user_id),
    #             params={"guild_id": str(self._config.guild_id), "with_mutual_guilds": "false"},
    #         )
    #     except Exception:  # noqa: BLE001 - best-effort; any failure just means "no pronouns"
    #         logger.debug("[discord:%s] couldn't fetch profile for %s", self.connector_id, user_id, exc_info=True)
    #         return None
    #     for section in ("guild_member_profile", "user_profile"):
    #         pronouns = (data.get(section) or {}).get("pronouns") if isinstance(data, dict) else None
    #         if pronouns:
    #             return str(pronouns).strip() or None
    #     return None

    async def _handle_raw_message_edit(self, payload: discord.RawMessageUpdateEvent) -> None:
        """MESSAGE_UPDATE covers three cases the bridge cares about:

        - a **pin toggle** - a minimal payload (`{id, channel_id, guild_id,
          pinned}`); Discord has a `pins_add` system message but no
          `pins_remove` one, so this is the only event covering both
          directions. Emitted as a `StandardPin` whenever `pinned` is present.
        - a **content edit** by the message's author - the payload carries a
          fresh `content` and an `edited_timestamp`. Emitted as a
          `StandardEdit` so `BridgeCoordinator` can sync every relayed copy.
        - an **auto-embed** update (a link Discord just unfurled) - carries
          `embeds` but no `edited_timestamp`; ignored, so a bare link paste
          doesn't tag every destination "(edited)".

        A webhook-authored edit (our own relayed copy being synced) is dropped
        here - detected cache-free via the payload's `webhook_id`, so it holds
        even for an uncached message where `payload.message.author` isn't
        populated; `BridgeCoordinator` suppresses the echo as a further
        backstop.
        """
        if payload.guild_id != self._config.guild_id:
            return
        data = payload.data or {}
        if "pinned" in data:
            if self._on_pin is not None:
                await self._on_pin(
                    StandardPin(
                        origin_connector_id=self.connector_id,
                        origin_channel_id=str(payload.channel_id),
                        origin_message_id=str(payload.message_id),
                        pinned=bool(data["pinned"]),
                    )
                )
            return
        if self._on_edit is None or not data.get("edited_timestamp") or "content" not in data:
            return
        if data.get("webhook_id"):
            return  # our own relayed webhook copy being edited - echo, always dropped
        author = data.get("author") or {}
        if author.get("bot") and not await self._bot_is_whitelisted(str(author.get("id") or "")):
            return
        await self._on_edit(
            StandardEdit(
                origin_connector_id=self.connector_id,
                origin_channel_id=str(payload.channel_id),
                origin_message_id=str(payload.message_id),
                new_content_markdown=data.get("content") or "",
                mentioned_users=_map_mentioned_users(getattr(payload, "message", None)),
                mentioned_roles=_map_mentioned_roles(getattr(payload, "message", None)),
                mentioned_channels=_map_mentioned_channels(getattr(payload, "message", None)),
            )
        )

    async def _handle_raw_message_delete(self, payload: discord.RawMessageDeleteEvent) -> None:
        """RAW_MESSAGE_DELETE's payload has no `webhook_id` (unlike
        MESSAGE_UPDATE), so the cache-free "is this our own webhook copy"
        drop `_handle_raw_message_edit` does isn't available here in the
        general case. `payload.cached_message` is used as a best-effort
        optimization when discord.py's message cache happens to hold it; the
        real (and sufficient) loop guard is `BridgeCoordinator`'s
        `_recent_deletes` TTL guard, since every delete the bridge itself
        issues is one exact, known (connector, channel, message_id) triple
        recorded right before the call."""
        if payload.guild_id != self._config.guild_id:
            return
        await self._emit_delete(payload.channel_id, payload.message_id, payload.cached_message)

    async def _handle_raw_bulk_message_delete(self, payload: discord.RawBulkMessageDeleteEvent) -> None:
        """RAW_BULK_MESSAGE_DELETE: emit one StandardDelete per deleted id,
        reusing `_handle_raw_message_delete`'s per-id cached-webhook check."""
        if payload.guild_id != self._config.guild_id:
            return
        cached_by_id = {message.id: message for message in payload.cached_messages}
        for message_id in payload.message_ids:
            await self._emit_delete(payload.channel_id, message_id, cached_by_id.get(message_id))

    async def _emit_delete(self, channel_id: int, message_id: int, cached_message: "discord.Message | None") -> None:
        if self._on_delete is None:
            return
        if cached_message is not None and cached_message.webhook_id is not None:
            return  # our own relayed webhook copy - best-effort skip
        await self._on_delete(
            StandardDelete(
                origin_connector_id=self.connector_id,
                origin_channel_id=str(channel_id),
                origin_message_id=str(message_id),
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

        Uses `mirror_channel_all_for_thread` rather than `mirror_channel_all`
        (issue #124): each destination's category placement is deferred until
        *after* the starter message is relayed (and pinned) below, so a
        connector whose `ensure_channel` bundles create+categorize into one
        (often slow) call - Stoat's, a whole-server-PATCH - doesn't finish
        organizing the channel before anything has had a chance to post into
        it.
        """
        if self._linker is None or thread.guild.id != self._config.guild_id:
            return
        parent = thread.parent
        if parent is None:
            return
        parent_bridged = await self._linker.is_linked(self.connector_id, str(parent.id))
        if (
            not parent_bridged
            and self._category_linker is not None
            and isinstance(parent, discord.ForumChannel)
        ):
            # A forum channel is linked as a *Category* (issue #100), not a
            # flat channel, so `is_linked` (channel mappings) misses it - its
            # posts still need mirroring, into that linked Category.
            parent_bridged = await self._category_linker.is_category_linked(
                self.connector_id, str(parent.id)
            )
        if not parent_bridged:
            return  # this thread's parent was never bridged - leave the thread alone

        self._pending_thread_starter[thread.id] = None
        try:
            result, finish_category_placements = await self._linker.mirror_channel_all_for_thread(
                local_connector=self.connector_id,
                local_channel_id=str(thread.id),
                local_channel_name=clip_name(thread.name),
                local_channel_category=clip_name(parent.name),
                category_from_channel_id=str(parent.id),
            )
        except MirrorInProgressError as exc:
            # A manual /mirror into one of the destinations is still running;
            # mirror_channel_all is all-or-nothing, so the thread isn't
            # mirrored anywhere this pass. Not an error - just log and bail.
            logger.info(
                "[discord:%s] deferred auto-mirror of thread %s: %s", self.connector_id, thread.id, exc
            )
            self._pending_thread_starter.pop(thread.id, None)
            self._thread_ready.discard(thread.id)
            return
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
                sender_color=self._resolve_sender_color(starter.author),
            )
            # Relay the starter message - and pin it on every destination it
            # landed on - *before* the mirrored channel is placed into its
            # thread Category below (issue #124): Stoat's ensure_channel does
            # a (often slow, whole-server-PATCH) category placement, and
            # posting first makes the starter message look like the first
            # thing that happened in the channel rather than something
            # dropped into an already-organized one.
            await self._on_message(replace(msg, origin_channel_id=str(thread.id), channel_name=thread.name))
            if self._on_pin is not None:
                await self._on_pin(
                    StandardPin(
                        origin_connector_id=self.connector_id,
                        origin_channel_id=str(thread.id),
                        origin_message_id=str(starter.id),
                        pinned=True,
                    )
                )

        # Deferred from mirror_channel_all_for_thread above - completes each
        # destination's category placement now that the starter message (if
        # any) has already landed in an uncategorized channel.
        for finish in finish_category_placements:
            await finish()

        await self._relay_thread_created_notice(thread, starter_author)

    async def _relay_thread_created_notice(self, thread: discord.Thread, starter_author: object) -> None:
        """Post a bot-authored "<user> started a thread: <#thread>" notice into
        the thread's parent channel, standing in for Discord's own system
        message (which _handle_message suppresses).

        Called only after the thread has been mirrored + linked, so each
        receiver's rewrite_channel_mentions can turn the `<#thread-id>` mention
        into its own linked copy of the mirrored channel (`#<thread name>` on
        IRC); `mentioned_channels` carries the thread name so it still falls
        back to `#<thread name>` if that lookup can't resolve (issue #84)."""
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
                mentioned_channels={str(thread.id): thread.name},
            )
        )

    async def _relay_thread_renamed_notice(self, message: discord.Message) -> None:
        """Post a bot-authored "<user> changed the post title/channel name:
        <new name>" notice into the thread/forum-post itself, standing in for
        Discord's own `channel_name_change` system message (which
        `_handle_message` suppresses) - and propagate the rename to every
        other connector's linked copy of this channel via `on_channel_renamed`
        (issue #152).

        `message.content` is the bare new name Discord populates this system
        message with (the "changed the ... title/name" wording is
        client-rendered from the message type, not stored content). The real
        renamer is embedded as a `<@id>` mention plus a `mentioned_users`
        entry so the existing `rewrite_mentions` pipeline resolves it to a
        `/link-user`-linked identity on the target, or falls back to the
        plain display name carried here. `message.content` is user-supplied
        text spliced straight into relayed content, so it's run through
        `_defang_mentions` the same way every other embedded display string
        is - a thread renamed to `@everyone` shouldn't relay a live mass
        ping (the bridge sets no `allowed_mentions` on its webhook sends).
        The rename propagated via `on_channel_renamed` below uses the real,
        un-defanged name - that value is a channel name on the target, never
        rendered as chat content."""
        thread = message.channel
        # thread.parent can be None (parent deleted/uncached) - isinstance
        # against None is just False, so this intentionally falls back to
        # the plain "channel name" wording rather than needing its own guard.
        wording = "post title" if isinstance(thread.parent, discord.ForumChannel) else "channel name"
        bot_user = self._client.user
        await self._on_message(
            StandardMessage(
                origin_connector_id=self.connector_id,
                origin_channel_id=str(thread.id),
                channel_name=getattr(thread, "name", str(thread.id)),
                sender_name=bot_user.display_name if bot_user is not None else "Bridge",
                sender_avatar_url=(
                    str(bot_user.display_avatar.url) if bot_user is not None and bot_user.display_avatar else None
                ),
                sender_user_id=str(bot_user.id) if bot_user is not None else "",
                content_markdown=f"<@{message.author.id}> changed the {wording}: {_defang_mentions(message.content)}",
                message_id=f"thread-renamed:{message.id}",
                mentioned_users={str(message.author.id): message.author.display_name},
            )
        )
        if self._on_channel_renamed is not None:
            await self._on_channel_renamed(self.connector_id, str(thread.id), message.content)

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
