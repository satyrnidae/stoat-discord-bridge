"""Discord sender/receiver services.

Instantiated once per configured Discord connector (config.yaml's `discord`
list can have any number of entries) since each guild needs its own
discord.Client/command tree.

Sender: a discord.Client that listens for messages in bridged channels and
emits StandardMessages.

Receiver: posts StandardMessages into Discord "as" the originating
Stoat/IRC user, via a per-channel Discord webhook (username + avatar
override) rather than the bridge bot's own identity. `target_channel_id`
is a real Discord channel id; the receiver resolves/creates that channel's
webhook itself (cached per channel), so `/link-channel` never needs an
admin to already have a webhook URL in hand.
"""

from __future__ import annotations

import logging
import re
from dataclasses import replace

import aiohttp
import discord
from discord import app_commands

from stoat_discord_bridge.admin_commands import (
    CategoryLinker,
    ChannelLinker,
    ConnectorInfo,
    EmoteLinker,
    LinkError,
    RoleLinker,
    UserLinker,
)
from stoat_discord_bridge.channel_structure import ChannelSpec, GroupSpec, GuildStructure, clip_name
from stoat_discord_bridge.config import DiscordConnectorConfig
from stoat_discord_bridge.models import (
    Attachment,
    CustomEmoji,
    StandardEmojiCreated,
    StandardEmojiDeleted,
    StandardMessage,
    StandardReaction,
)
from stoat_discord_bridge.services.base import (
    OnEmojiCreated,
    OnEmojiDeleted,
    OnMemberRolesChanged,
    OnChannelRolePermissionChanged,
    OnMessage,
    OnReaction,
    OnRoleDeleted,
    OnRoleRenamed,
    PartialRelayError,
    ReceiverService,
    SenderService,
)
from stoat_discord_bridge.services.formatting import (
    chunk_content,
    content_with_attachments,
)
from stoat_discord_bridge.services.mentions import (
    rewrite_channel_mentions,
    rewrite_mentions,
    rewrite_role_mentions,
)
from stoat_discord_bridge.services.role_sync import (
    NEUTRAL_PERMISSIONS,
    discord_overwrite_to_neutral,
    neutral_to_discord_pair,
)

_MAPPED_DISCORD_PERM_ATTRS = {d_attr for d_attr, _ in NEUTRAL_PERMISSIONS.values()}
from stoat_discord_bridge.status import HealthTracker
from stoat_discord_bridge.storage.channel_mappings import ChannelMappingRepository
from stoat_discord_bridge.storage.role_mappings import RoleMappingRepository
from stoat_discord_bridge.storage.user_mappings import UserMappingRepository

logger = logging.getLogger(__name__)

# Discord webhook hard limits: 2000 chars per message, 1-80 char usernames,
# and usernames may not contain "clyde" or "discord" (case-insensitive) or
# the API rejects the send outright.
_CONTENT_LIMIT = 2000
_USERNAME_LIMIT = 80
_FORBIDDEN_USERNAME_SUBSTRINGS = ("clyde", "discord")

_CHANNEL_MENTION_RE = re.compile(r"^<#(\d+)>$")
_ROLE_MENTION_RE = re.compile(r"^<@&(\d+)>$")


def _normalize_role_id(raw: str) -> str:
    """Strip a pasted `<@&id>` role mention down to the bare id; leave a bare
    id or a role name untouched (RoleLinker resolves a name itself)."""
    match = _ROLE_MENTION_RE.match(raw.strip())
    return match.group(1) if match else raw.strip()


def _connector_autocomplete_choices(
    current: str, connectors: dict[str, ConnectorInfo], *, include_all: bool = False
) -> list[app_commands.Choice[str]]:
    """Shared filtering behind every `service` option's
    autocomplete: Discord expects results ranked/filtered by `current` (the
    option's in-progress text) and caps them at 25 - substring match against
    both the connector id and its display label, so typing either finds it.
    `include_all` adds the literal "all" choice `/mirror-channel` accepts."""
    current = current.lower()
    choices = [
        app_commands.Choice(name=f"{info.label} ({connector_id})", value=connector_id)
        for connector_id, info in connectors.items()
        if current in connector_id.lower() or current in info.label.lower()
    ]
    if include_all and current in "all":
        choices.insert(0, app_commands.Choice(name="all", value="all"))
    return choices[:25]


def _normalize_channel_id(raw: str) -> str:
    """The `external_id`/`local_id` slash-command
    options below are plain strings, not discord.py channel-type options -
    Discord's client still lets a user pick a channel from the `#` picker
    while typing one, which pastes a full `<#id>` mention rather than the
    bare id. Strip that down to the id so it's actually usable as one -
    otherwise it ends up stored as a channel_id that never matches a real
    incoming message's origin_channel_id, and (for /mirror-channel, which
    also uses this as the display name when no name can be resolved) as the
    literal name of the channel created on the other connector."""
    match = _CHANNEL_MENTION_RE.match(raw.strip())
    return match.group(1) if match else raw


class _DiscordClient(discord.Client):
    """discord.py dispatches events by looking up `on_<event>` attributes on
    the Client instance itself, so *something* has to subclass discord.Client.
    This subclass exists only to satisfy that and delegates every callback to
    the owning DiscordSenderService, which otherwise doesn't need to inherit
    from a third-party client class."""

    def __init__(self, owner: DiscordSenderService) -> None:
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

    async def on_thread_create(self, thread: discord.Thread) -> None:
        await self._owner._handle_thread_create(thread)

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


class DiscordSenderService(SenderService):
    def __init__(
        self,
        config: DiscordConnectorConfig,
        on_message: OnMessage,
        health: HealthTracker,
        on_reaction: OnReaction | None = None,
        on_emoji_created: OnEmojiCreated | None = None,
        on_emoji_deleted: OnEmojiDeleted | None = None,
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
        SenderService.__init__(self, on_message, on_reaction, on_emoji_created, on_emoji_deleted)
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
        self._guild = discord.Object(id=config.guild_id)
        self._client = _DiscordClient(self)
        self.tree = discord.app_commands.CommandTree(self._client)

        @self.tree.command(
            name="status", description="Show sync target health (Discord/Stoat/IRC)", guild=self._guild
        )
        async def status_command(interaction: discord.Interaction) -> None:
            await interaction.response.send_message(self._health.render(), ephemeral=True)

        @self.tree.command(
            name="linked-channels",
            description="List every channel linked to this one across the bridge",
            guild=self._guild,
        )
        async def linked_channels_command(interaction: discord.Interaction) -> None:
            await self._handle_linked_channels(interaction)

        @self.tree.command(
            name="linked-categories",
            description="List every Category linked to this channel's Category across the bridge",
            guild=self._guild,
        )
        async def linked_categories_command(interaction: discord.Interaction) -> None:
            await self._handle_linked_categories(interaction)

        async def link_channel_service_autocomplete(
            interaction: discord.Interaction, current: str
        ) -> list[app_commands.Choice[str]]:
            connectors = self._linker.connectors if self._linker is not None else {}
            return _connector_autocomplete_choices(current, connectors)

        @self.tree.command(
            name="link-channel",
            description="Link a channel from another bridge connector to this channel",
            guild=self._guild,
        )
        @app_commands.default_permissions(manage_guild=True)
        @app_commands.describe(
            service="Connector id to link from (see /status for configured connectors)",
            external_id="Channel id on that connector",
            local_id="Channel id on this connector (defaults to the current channel)",
        )
        @app_commands.autocomplete(service=link_channel_service_autocomplete)
        async def link_channel_command(
            interaction: discord.Interaction, service: str, external_id: str, local_id: str | None = None
        ) -> None:
            await self._handle_link_channel(interaction, service, external_id, local_id)

        async def link_category_service_autocomplete(
            interaction: discord.Interaction, current: str
        ) -> list[app_commands.Choice[str]]:
            connectors = self._category_linker.connectors if self._category_linker is not None else {}
            return _connector_autocomplete_choices(current, connectors)

        @self.tree.command(
            name="link-category",
            description="Link a Category from another connector; new channels in either side sync automatically",
            guild=self._guild,
        )
        @app_commands.default_permissions(manage_guild=True)
        @app_commands.describe(
            service="Connector id to link from (see /status for configured connectors)",
            external_id="Category id on that connector",
            local_id="Category id on this connector (defaults to the current channel's Category)",
        )
        @app_commands.autocomplete(service=link_category_service_autocomplete)
        async def link_category_command(
            interaction: discord.Interaction, service: str, external_id: str, local_id: str | None = None
        ) -> None:
            await self._handle_link_category(interaction, service, external_id, local_id)

        async def link_emote_service_autocomplete(
            interaction: discord.Interaction, current: str
        ) -> list[app_commands.Choice[str]]:
            connectors = self._emote_linker.connectors if self._emote_linker is not None else {}
            return _connector_autocomplete_choices(current, connectors)

        @self.tree.command(
            name="link-emote",
            description="Link a custom emoji from another bridge connector to a local custom emoji",
            guild=self._guild,
        )
        @app_commands.default_permissions(manage_guild=True)
        @app_commands.describe(
            service="Connector id to link from (see /status for configured connectors)",
            external_id="Emoji id on that connector",
            local_id="Emoji id on this connector",
        )
        @app_commands.autocomplete(service=link_emote_service_autocomplete)
        async def link_emote_command(
            interaction: discord.Interaction, service: str, external_id: str, local_id: str
        ) -> None:
            await self._handle_link_emote(interaction, service, external_id, local_id)

        async def mirror_channel_service_autocomplete(
            interaction: discord.Interaction, current: str
        ) -> list[app_commands.Choice[str]]:
            connectors = self._linker.connectors if self._linker is not None else {}
            return _connector_autocomplete_choices(current, connectors, include_all=True)

        @self.tree.command(
            name="mirror-channel",
            description="Ensure a linked counterpart of a channel exists on another connector (or all of them)",
            guild=self._guild,
        )
        @app_commands.default_permissions(manage_guild=True)
        @app_commands.describe(
            service="Connector id to mirror to, or 'all' for every configured connector",
            local_id="Channel id on this connector (defaults to the current channel)",
        )
        @app_commands.autocomplete(service=mirror_channel_service_autocomplete)
        async def mirror_channel_command(
            interaction: discord.Interaction, service: str, local_id: str | None = None
        ) -> None:
            await self._handle_mirror_channel(interaction, service, local_id)

        async def unlink_channel_service_autocomplete(
            interaction: discord.Interaction, current: str
        ) -> list[app_commands.Choice[str]]:
            connectors = self._linker.connectors if self._linker is not None else {}
            return _connector_autocomplete_choices(current, connectors, include_all=True)

        @self.tree.command(
            name="unlink-channel",
            description="Unlink a channel's bridge - one connector, or the whole group (default: all)",
            guild=self._guild,
        )
        @app_commands.default_permissions(manage_guild=True)
        @app_commands.describe(
            service="Connector id to unlink, or 'all' to dissolve the whole bridge group (default: all)",
            local_id="Channel id on this connector (defaults to the current channel)",
        )
        @app_commands.autocomplete(service=unlink_channel_service_autocomplete)
        async def unlink_channel_command(
            interaction: discord.Interaction, service: str | None = None, local_id: str | None = None
        ) -> None:
            await self._handle_unlink_channel(interaction, service, local_id)

        async def unlink_category_service_autocomplete(
            interaction: discord.Interaction, current: str
        ) -> list[app_commands.Choice[str]]:
            connectors = self._category_linker.connectors if self._category_linker is not None else {}
            return _connector_autocomplete_choices(current, connectors, include_all=True)

        @self.tree.command(
            name="unlink-category",
            description="Unlink this channel's Category's bridge - one connector, or the whole group (default: all)",
            guild=self._guild,
        )
        @app_commands.default_permissions(manage_guild=True)
        @app_commands.describe(
            service="Connector id to unlink, or 'all' to dissolve the whole bridge group (default: all)",
        )
        @app_commands.autocomplete(service=unlink_category_service_autocomplete)
        async def unlink_category_command(interaction: discord.Interaction, service: str | None = None) -> None:
            await self._handle_unlink_category(interaction, service)

        # Roles and users use the `/link …`, `/unlink …`, `/linked …`,
        # `/mirror …` subcommand form (app_commands groups); channel, category
        # and emote still use the flat `/link-channel` etc. names - a later
        # step migrates those onto the same shape.
        def _linker_service_autocomplete(get_linker, *, include_all: bool):
            async def _ac(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
                linker = get_linker()
                connectors = linker.connectors if linker is not None else {}
                return _connector_autocomplete_choices(current, connectors, include_all=include_all)

            return _ac

        def role_service_autocomplete(*, include_all: bool):
            return _linker_service_autocomplete(lambda: self._role_linker, include_all=include_all)

        def user_service_autocomplete(*, include_all: bool):
            return _linker_service_autocomplete(lambda: self._user_linker, include_all=include_all)

        _manage = discord.Permissions(manage_guild=True)
        link_group = app_commands.Group(
            name="link", description="Link an entity across the bridge", default_permissions=_manage
        )
        unlink_group = app_commands.Group(
            name="unlink", description="Unlink an entity's bridge", default_permissions=_manage
        )
        linked_group = app_commands.Group(name="linked", description="List cross-bridge links")
        mirror_group = app_commands.Group(
            name="mirror", description="Create+link a matching entity elsewhere", default_permissions=_manage
        )
        for _g in (link_group, unlink_group, linked_group, mirror_group):
            self.tree.add_command(_g, guild=self._guild)

        @link_group.command(name="role", description="Link a role from another connector to a local role")
        @app_commands.describe(
            local_id="Role id or name on this connector",
            service="Connector id to link from",
            external_id="Role id or name on that connector",
        )
        @app_commands.autocomplete(service=role_service_autocomplete(include_all=False))
        async def link_role_command(
            interaction: discord.Interaction, local_id: str, service: str, external_id: str
        ) -> None:
            await self._handle_link_role(interaction, local_id, service, external_id)

        @unlink_group.command(name="role", description="Unlink a role - one connector, or the whole group (default: all)")
        @app_commands.describe(
            local_id="Role id or name on this connector",
            service="Connector id to unlink, or 'all' (default: all)",
        )
        @app_commands.autocomplete(service=role_service_autocomplete(include_all=True))
        async def unlink_role_command(
            interaction: discord.Interaction, local_id: str, service: str | None = None
        ) -> None:
            await self._handle_unlink_role(interaction, local_id, service)

        @linked_group.command(name="roles", description="List roles linked across the bridge (omit the role to list all)")
        @app_commands.describe(local_id="Role id or name on this connector (omit to list every linked role)")
        async def linked_roles_command(
            interaction: discord.Interaction, local_id: str | None = None
        ) -> None:
            await self._handle_linked_roles(interaction, local_id, None)

        @mirror_group.command(name="role", description="Ensure a linked counterpart of a role exists elsewhere")
        @app_commands.describe(
            local_id="Role id or name on this connector",
            service="Connector id to mirror to, or 'all' (default: all)",
        )
        @app_commands.autocomplete(service=role_service_autocomplete(include_all=True))
        async def mirror_role_command(
            interaction: discord.Interaction, local_id: str, service: str | None = None
        ) -> None:
            await self._handle_mirror_role(interaction, local_id, service)

        @link_group.command(
            name="user",
            description="Link a user from another connector to a local member, for mentions and masquerade override",
        )
        @app_commands.describe(
            service="Connector id to link from (see /status for configured connectors)",
            external_id="User id or display name on that connector",
            local_id="The Discord member this is the same person as",
        )
        @app_commands.autocomplete(service=user_service_autocomplete(include_all=False))
        async def link_user_command(
            interaction: discord.Interaction, service: str, external_id: str, local_id: discord.Member
        ) -> None:
            await self._handle_link_user(interaction, service, external_id, local_id)

        @unlink_group.command(
            name="user", description="Unlink a user's cross-connector identity - one connector, or the whole group (default: all)"
        )
        @app_commands.describe(
            service="Connector id to unlink, or 'all' to dissolve the whole link group (default: all)",
            local_id="Member to unlink (defaults to yourself)",
        )
        @app_commands.autocomplete(service=user_service_autocomplete(include_all=True))
        async def unlink_user_command(
            interaction: discord.Interaction, service: str | None = None, local_id: discord.Member | None = None
        ) -> None:
            await self._handle_unlink_user(interaction, service, local_id)

        @linked_group.command(
            name="users", description="List cross-connector user links, for debugging - or just one member's, if given"
        )
        @app_commands.describe(local_id="Show only this member's link (omit to list every linked user)")
        async def linked_users_command(
            interaction: discord.Interaction, local_id: discord.Member | None = None
        ) -> None:
            await self._handle_linked_users(interaction, local_id)

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
        await self._on_message(_to_standard_message(message, self.connector_id))

    async def _handle_thread_create(self, thread: discord.Thread) -> None:
        """A Discord thread (including a forum post, also a discord.Thread -
        see _get_or_create_webhook's docstring) has no IRC/Stoat equivalent,
        so instead of relaying its starter message as plain text, bundle a
        `/mirror-channel all`-style request: ensure a same-named channel
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
        general "mirror the source's own Category" rule /mirror-channel
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
            msg = _to_standard_message(starter, self.connector_id)
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
        await self._on_reaction(_to_standard_reaction(payload, self.connector_id, added=added))

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

    async def start(self) -> None:
        await self._client.start(self._config.bot_token)

    async def close(self) -> None:
        await self._client.close()

    async def get_channel_name(self, channel_id: str) -> str | None:
        """Best-effort channel-id -> name lookup, used as this connector's
        `ConnectorInfo.resolve_channel_name` for `/link-channel`."""
        try:
            channel = self._client.get_channel(int(channel_id)) or await self._client.fetch_channel(int(channel_id))
        except (discord.HTTPException, discord.NotFound, ValueError):
            logger.debug("[discord:%s] couldn't resolve channel name for %s", self.connector_id, channel_id)
            return None
        return getattr(channel, "name", None)

    async def get_channel_category_name(self, channel_id: str) -> str | None:
        """Best-effort channel-id -> Category-name lookup, used by
        `/mirror-channel` to carry a channel's Category across to the
        destination connector. Catches broadly (not just discord.py's own
        HTTPException/NotFound) since an id that isn't a real channel this
        client can see - e.g. a bare Stoat/IRC-style name typed into
        /mirror-channel's local_id option - can fail in ways short
        of a clean discord.py exception (fetch_channel(), unlike
        get_channel_name's other call sites, isn't otherwise guarded here)."""
        try:
            channel = self._client.get_channel(int(channel_id)) or await self._client.fetch_channel(int(channel_id))
            category = getattr(channel, "category", None)
        except Exception:
            logger.debug("[discord:%s] couldn't resolve channel category for %s", self.connector_id, channel_id)
            return None
        return category.name if category is not None else None

    async def get_category_name(self, category_id: str) -> str | None:
        """Best-effort Category-id -> name lookup, used as this connector's
        `ConnectorInfo.resolve_category_name` for `/link-category`'s
        destination-name resolution. Discord Categories are themselves
        channels (`discord.CategoryChannel`), so the same get_channel-or-
        fetch_channel pattern as get_channel_name resolves them directly."""
        try:
            category = self._client.get_channel(int(category_id)) or await self._client.fetch_channel(int(category_id))
        except Exception:
            logger.debug("[discord:%s] couldn't resolve category name for %s", self.connector_id, category_id)
            return None
        return getattr(category, "name", None)

    async def get_user_name(self, user_id: str) -> str | None:
        """Best-effort user-id -> display-name lookup, used as this
        connector's `ConnectorInfo.resolve_user_name` for `/linked-users`."""
        try:
            user = self._client.get_user(int(user_id)) or await self._client.fetch_user(int(user_id))
        except (discord.HTTPException, discord.NotFound, ValueError):
            logger.debug("[discord:%s] couldn't resolve user name for %s", self.connector_id, user_id)
            return None
        return getattr(user, "display_name", None)

    async def resolve_user_id_by_name(self, token: str) -> str | None:
        """Resolve a bare display name / global name / username to a member
        id so `/link user` etc. accept either. A token that's already a real
        member id is returned as-is; an unrecognized token yields None
        (UserLinker then treats it as a literal id). Case-insensitive; first
        match wins. Needs the privileged members intent (enabled - see
        CLAUDE.md) for guild.members to be populated."""
        guild = self._guild_or_none()
        if guild is None:
            return None
        if token.isdigit() and guild.get_member(int(token)) is not None:
            return token
        lowered = token.casefold()
        for member in guild.members:
            names = (member.display_name, getattr(member, "global_name", None), member.name)
            if any(n is not None and n.casefold() == lowered for n in names):
                return str(member.id)
        return None

    def _guild_or_none(self) -> "discord.Guild | None":
        return self._client.get_guild(self._config.guild_id)

    async def get_role_name(self, role_id: str) -> str | None:
        """Best-effort role-id -> name lookup, this connector's
        `ConnectorInfo.resolve_role_name`."""
        guild = self._guild_or_none()
        if guild is None:
            return None
        try:
            role = guild.get_role(int(role_id))
        except ValueError:
            return None
        return role.name if role is not None else None

    async def resolve_role_id_by_name(self, token: str) -> str | None:
        """Resolve a bare role name to its id so `/link-role` etc. accept
        either. A token that's already a real role id is returned as-is; an
        unrecognized token yields None (RoleLinker then treats it as a
        literal id). Case-insensitive; first match wins (Discord role names
        aren't unique)."""
        guild = self._guild_or_none()
        if guild is None:
            return None
        if token.isdigit() and guild.get_role(int(token)) is not None:
            return token
        lowered = token.casefold()
        for role in guild.roles:
            if role.name.casefold() == lowered:
                return str(role.id)
        return None

    async def ensure_role(self, name: str) -> str:
        """Get-or-create a role named `name`, returning its id - this
        connector's `ConnectorInfo.ensure_role` for `/mirror role`."""
        guild = self._guild_or_none()
        if guild is None:
            raise RuntimeError("Discord guild isn't cached yet - the bridge may still be connecting")
        lowered = name.casefold()
        for role in guild.roles:
            if role.name.casefold() == lowered:
                return str(role.id)
        role = await guild.create_role(name=name, reason="bridge role mirror")
        return str(role.id)

    def snapshot_guild_structure(self) -> GuildStructure:
        """Build a platform-neutral snapshot of the bridged guild's current
        categories/channels, for the Stoat `/mirror-channels` command.

        Reads from discord.py's gateway cache (populated by the time
        `on_ready` fires), so this makes no Discord API calls of its own.
        Forum posts are limited to what's currently active in cache —
        archived posts aren't paged in.
        """
        guild = self._client.get_guild(self._config.guild_id)
        if guild is None:
            raise RuntimeError("Discord guild isn't cached yet — the bridge may still be connecting")

        groups: list[GroupSpec] = []
        for category in guild.categories:
            channels = [
                ChannelSpec(name=clip_name(ch.name), source_channel_id=str(ch.id))
                for ch in category.channels
                if isinstance(ch, (discord.TextChannel, discord.VoiceChannel))
            ]
            if channels:
                groups.append(GroupSpec(name=clip_name(category.name), channels=channels))

        groups.extend(
            GroupSpec(
                name=clip_name(forum.name),
                channels=[
                    ChannelSpec(name=clip_name(thread.name), source_channel_id=str(thread.id))
                    for thread in forum.threads
                ],
            )
            for forum in guild.forums
        )

        ungrouped = [
            ChannelSpec(name=clip_name(ch.name), source_channel_id=str(ch.id))
            for ch in (*guild.text_channels, *guild.voice_channels)
            if ch.category is None
        ]

        return GuildStructure(groups=groups, ungrouped_channels=ungrouped)

    async def _handle_linked_channels(self, interaction: discord.Interaction) -> None:
        if self._linker is None:
            await interaction.response.send_message("Linking isn't configured.", ephemeral=True)
            return
        summary = await self._linker.list_linked_channels(
            local_connector=self.connector_id, local_channel_id=str(interaction.channel_id)
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
            "[discord:%s] %s ran /link-channel service=%s external_id=%s local_id=%s",
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
            logger.info("[discord:%s] /link-channel rejected: %s", self.connector_id, exc)
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        await interaction.response.send_message(summary, ephemeral=True)

    async def _handle_linked_categories(self, interaction: discord.Interaction) -> None:
        if self._category_linker is None:
            await interaction.response.send_message("Category linking isn't configured.", ephemeral=True)
            return
        category = getattr(interaction.channel, "category", None)
        if category is None:
            await interaction.response.send_message("This channel isn't inside a Category.", ephemeral=True)
            return
        summary = await self._category_linker.list_linked_categories(
            local_connector=self.connector_id, local_category_id=str(category.id)
        )
        await interaction.response.send_message(summary, ephemeral=True)

    async def _handle_link_category(
        self, interaction: discord.Interaction, service: str, external_id: str, local_id: str | None
    ) -> None:
        if self._category_linker is None:
            await interaction.response.send_message("Category linking isn't configured.", ephemeral=True)
            return
        category = getattr(interaction.channel, "category", None)
        if category is None:
            await interaction.response.send_message("This channel isn't inside a Category.", ephemeral=True)
            return
        external_id = _normalize_channel_id(external_id)
        if local_id is not None:
            local_id = _normalize_channel_id(local_id)
        logger.info(
            "[discord:%s] %s ran /link-category service=%s external_id=%s local_id=%s",
            self.connector_id,
            interaction.user.id,
            service,
            external_id,
            local_id,
        )
        try:
            summary = await self._category_linker.link_category(
                local_connector=self.connector_id,
                local_category_id=str(category.id),
                local_category_name=category.name,
                source=service,
                source_id=external_id,
                destination_id=local_id,
            )
        except LinkError as exc:
            logger.info("[discord:%s] /link-category rejected: %s", self.connector_id, exc)
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        await interaction.response.send_message(summary, ephemeral=True)

    async def _handle_unlink_category(self, interaction: discord.Interaction, service: str | None) -> None:
        if self._category_linker is None:
            await interaction.response.send_message("Category linking isn't configured.", ephemeral=True)
            return
        category = getattr(interaction.channel, "category", None)
        if category is None:
            await interaction.response.send_message("This channel isn't inside a Category.", ephemeral=True)
            return
        logger.info(
            "[discord:%s] %s ran /unlink-category service=%s",
            self.connector_id,
            interaction.user.id,
            service,
        )
        try:
            summary = await self._category_linker.unlink_category(
                local_connector=self.connector_id, local_category_id=str(category.id), destination=service
            )
        except LinkError as exc:
            logger.info("[discord:%s] /unlink-category rejected: %s", self.connector_id, exc)
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        await interaction.response.send_message(summary, ephemeral=True)

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

    async def _handle_link_emote(
        self, interaction: discord.Interaction, service: str, external_id: str, local_id: str
    ) -> None:
        if self._emote_linker is None:
            await interaction.response.send_message("Linking isn't configured.", ephemeral=True)
            return
        logger.info(
            "[discord:%s] %s ran /link-emote service=%s external_id=%s local_id=%s",
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
            logger.info("[discord:%s] /link-emote rejected: %s", self.connector_id, exc)
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
        self, interaction: discord.Interaction, service: str, local_id: str | None
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
            "[discord:%s] %s ran /mirror-channel service=%s local_id=%s",
            self.connector_id,
            interaction.user.id,
            service,
            channel_id,
        )
        try:
            if service.lower() == "all":
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
            logger.info("[discord:%s] /mirror-channel rejected: %s", self.connector_id, exc)
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        await interaction.response.send_message(summary, ephemeral=True)

    async def _handle_unlink_channel(
        self, interaction: discord.Interaction, service: str | None, local_id: str | None
    ) -> None:
        if self._linker is None:
            await interaction.response.send_message("Linking isn't configured.", ephemeral=True)
            return
        channel_id = _normalize_channel_id(local_id) if local_id is not None else str(interaction.channel_id)
        logger.info(
            "[discord:%s] %s ran /unlink-channel service=%s local_id=%s",
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
            logger.info("[discord:%s] /unlink-channel rejected: %s", self.connector_id, exc)
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


class DiscordReceiverService(ReceiverService):
    supports_reactions = True
    supports_emoji = True

    def __init__(
        self,
        client: discord.Client,
        guild_id: int,
        connector_id: str,
        user_mappings: UserMappingRepository | None = None,
        enable_local_user_masquerade: bool = True,
        channel_mappings: ChannelMappingRepository | None = None,
        role_mappings: RoleMappingRepository | None = None,
    ) -> None:
        self._client = client
        self._guild_id = guild_id
        self.connector_id = connector_id
        self._user_mappings = user_mappings
        self._channel_mappings = channel_mappings
        self._role_mappings = role_mappings
        self._enable_local_user_masquerade = enable_local_user_masquerade
        self._session: aiohttp.ClientSession | None = None
        self._webhooks: dict[str, discord.Webhook] = {}

    async def receive(self, message: StandardMessage, *, target_channel_id: str) -> list[str]:
        webhook, thread = await self._get_or_create_webhook(target_channel_id)
        sender_name = message.sender_name
        avatar_url = message.sender_avatar_url
        if self._user_mappings is not None and self._enable_local_user_masquerade:
            local_identity = await self._resolve_local_identity(message)
            if local_identity is not None:
                sender_name, avatar_url = local_identity
        elif self._user_mappings is not None:
            logger.debug(
                "[discord:%s] local user masquerade disabled (enable_local_user_masquerade=false), "
                "not resolving local identity for sender %s",
                self.connector_id,
                message.sender_user_id,
            )
        username = _sanitize_username(sender_name)
        content = content_with_attachments(message)
        if self._user_mappings is not None:
            content = await rewrite_mentions(
                content,
                origin_connector_id=message.origin_connector_id,
                target_connector_id=self.connector_id,
                target_kind="discord",
                user_mappings=self._user_mappings,
            )
        if self._channel_mappings is not None:
            content = await rewrite_channel_mentions(
                content,
                origin_connector_id=message.origin_connector_id,
                target_connector_id=self.connector_id,
                target_kind="discord",
                channel_mappings=self._channel_mappings,
            )
        if self._role_mappings is not None:
            content = await rewrite_role_mentions(
                content,
                origin_connector_id=message.origin_connector_id,
                target_connector_id=self.connector_id,
                target_kind="discord",
                role_mappings=self._role_mappings,
            )
        ids: list[str] = []
        for chunk in chunk_content(content, _CONTENT_LIMIT):
            logger.debug(
                "[discord:%s] sending webhook message to channel %s as %r (avatar_url=%r): %r",
                self.connector_id,
                target_channel_id,
                username,
                avatar_url,
                chunk,
            )
            try:
                sent = await webhook.send(
                    content=chunk,
                    username=username,
                    avatar_url=avatar_url,
                    wait=True,
                    **({"thread": thread} if thread is not None else {}),
                )
            except Exception as exc:
                raise PartialRelayError(ids, exc) from exc
            logger.debug("[discord:%s] webhook message sent, id=%s", self.connector_id, sent.id)
            ids.append(str(sent.id))
        return ids

    async def add_reaction(self, *, target_channel_id: str, target_message_id: str, emoji: str | CustomEmoji) -> None:
        message = await self._get_partial_message(target_channel_id, target_message_id)
        await message.add_reaction(_to_discord_emoji(emoji))

    async def remove_reaction(
        self, *, target_channel_id: str, target_message_id: str, emoji: str | CustomEmoji
    ) -> None:
        message = await self._get_partial_message(target_channel_id, target_message_id)
        await message.remove_reaction(_to_discord_emoji(emoji), self._client.user)

    async def create_emoji(self, emoji: CustomEmoji) -> CustomEmoji | None:
        guild = self._client.get_guild(self._guild_id)
        if guild is None:
            return None
        if self._session is None:
            self._session = aiohttp.ClientSession()
        try:
            async with self._session.get(emoji.image_url) as resp:
                resp.raise_for_status()
                image_bytes = await resp.read()
            created = await guild.create_custom_emoji(name=_sanitize_emoji_name(emoji.name), image=image_bytes)
        except (discord.HTTPException, aiohttp.ClientError) as exc:
            logger.warning(
                "[discord:%s] couldn't create emoji %r in guild %s: %s", self.connector_id, emoji.name, guild.id, exc
            )
            return None  # emoji slots full, name taken, image too large, etc. - skip this platform
        return CustomEmoji(
            native_id=str(created.id), name=created.name, image_url=str(created.url), animated=created.animated
        )

    async def _resolve_local_identity(self, message: StandardMessage) -> tuple[str, str | None] | None:
        """If `message`'s sender is linked (via /link-user) to a Discord
        identity on this connector, return that identity's (display_name,
        avatar_url) to masquerade as instead of the remote sender's own -
        None if unlinked or the linked id can't be resolved at all. Prefers
        the guild Member (whose server nickname/avatar override is what
        `_to_standard_message` uses for a *native* Discord message's own
        sender_name/avatar) over the global User, which only has an
        account-wide username/avatar - using the User here would show a
        linked user's username instead of their nickname in this guild."""
        local_user_id = await self._user_mappings.find_linked_user_id(
            message.origin_connector_id, message.sender_user_id, self.connector_id
        )
        if local_user_id is None:
            return None
        identity = await self._fetch_member(local_user_id) or await self._fetch_user(local_user_id)
        if identity is None:
            logger.warning(
                "[discord:%s] local user masquerade failed: linked user %s couldn't be resolved to a "
                "guild member or a global user",
                self.connector_id,
                local_user_id,
            )
            return None
        name = getattr(identity, "display_name", None)
        if not name:
            logger.warning(
                "[discord:%s] local user masquerade failed: linked user %s resolved but has no usable display name",
                self.connector_id,
                local_user_id,
            )
            return None
        avatar = getattr(identity, "display_avatar", None)
        avatar_url = str(avatar.url) if avatar else None
        logger.debug(
            "[discord:%s] resolved local user masquerade identity for %s: name=%r avatar_url=%r",
            self.connector_id,
            local_user_id,
            name,
            avatar_url,
        )
        return name, avatar_url

    async def _fetch_member(self, user_id: str) -> discord.Member | None:
        guild = self._client.get_guild(self._guild_id)
        if guild is None:
            return None
        try:
            return guild.get_member(int(user_id)) or await guild.fetch_member(int(user_id))
        except (discord.HTTPException, discord.NotFound, ValueError):
            return None

    async def _fetch_user(self, user_id: str) -> discord.User | None:
        try:
            return self._client.get_user(int(user_id)) or await self._client.fetch_user(int(user_id))
        except (discord.HTTPException, discord.NotFound, ValueError):
            return None

    async def _get_or_create_webhook(self, channel_id: str) -> tuple[discord.Webhook, discord.Thread | None]:
        # Threads have no webhooks of their own - a webhook belongs to (and
        # is fetched/created on) the thread's parent channel, and posting
        # into the thread itself is done by passing thread= to
        # Webhook.send() below. Cache under the parent's id so every thread
        # under it shares one webhook instead of creating a new one each.
        channel = self._client.get_channel(int(channel_id)) or await self._client.fetch_channel(int(channel_id))
        thread = channel if isinstance(channel, discord.Thread) else None
        webhook_channel = channel.parent if thread is not None else channel
        cache_key = str(webhook_channel.id)
        webhook = self._webhooks.get(cache_key)
        if webhook is not None:
            return webhook, thread
        existing = next((w for w in await webhook_channel.webhooks() if w.user == self._client.user), None)
        if existing is not None:
            webhook = existing
        else:
            # A per-message avatar_url override (the relayed sender's own
            # avatar) is passed to webhook.send() below when available, but
            # when it's not (e.g. sender_avatar_url couldn't be resolved),
            # Discord falls back to the webhook's own avatar - give it the
            # bot's, rather than Discord's blank/generic default, so an
            # unattributable message still looks like it came from *this*
            # bridge rather than nothing at all.
            avatar_bytes = await self._client.user.display_avatar.read()
            webhook = await webhook_channel.create_webhook(name="Bridge", avatar=avatar_bytes)
            logger.info("[discord:%s] created bridge webhook in channel %s", self.connector_id, cache_key)
        self._webhooks[cache_key] = webhook
        return webhook, thread

    async def _get_partial_message(self, target_channel_id: str, target_message_id: str) -> discord.PartialMessage:
        channel = self._client.get_channel(int(target_channel_id)) or await self._client.fetch_channel(
            int(target_channel_id)
        )
        return channel.get_partial_message(int(target_message_id))

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None


def _to_standard_message(message: discord.Message, connector_id: str) -> StandardMessage:
    return StandardMessage(
        origin_connector_id=connector_id,
        origin_channel_id=str(message.channel.id),
        channel_name=getattr(message.channel, "name", str(message.channel.id)),
        sender_name=message.author.display_name,
        sender_avatar_url=str(message.author.display_avatar.url) if message.author.display_avatar else None,
        sender_user_id=str(message.author.id),
        content_markdown=message.content,
        message_id=str(message.id),
        attachments=[
            Attachment(url=a.url, filename=a.filename, content_type=a.content_type, size_bytes=a.size)
            for a in message.attachments
        ],
    )


def _to_standard_reaction(payload: discord.RawReactionActionEvent, connector_id: str, *, added: bool) -> StandardReaction:
    emoji = payload.emoji
    emoji_repr: str | CustomEmoji
    if emoji.is_custom_emoji():
        emoji_repr = CustomEmoji(
            native_id=str(emoji.id), name=emoji.name or "", image_url=str(emoji.url), animated=emoji.animated
        )
    else:
        emoji_repr = emoji.name  # plain unicode emoji
    return StandardReaction(
        origin_connector_id=connector_id,
        origin_channel_id=str(payload.channel_id),
        origin_message_id=str(payload.message_id),
        emoji=emoji_repr,
        added=added,
    )


def _to_discord_emoji(emoji: str | CustomEmoji) -> str | discord.PartialEmoji:
    if isinstance(emoji, str):
        return emoji
    return discord.PartialEmoji(name=emoji.name, id=int(emoji.native_id), animated=emoji.animated)


def _sanitize_emoji_name(name: str) -> str:
    """Coerce an inbound emoji name into Discord's rules: 2-32 chars, alphanumeric/underscore only."""
    name = re.sub(r"[^A-Za-z0-9_]", "_", name).strip("_") or "emoji"
    name = name[:32]
    return name if len(name) >= 2 else name.ljust(2, "_")


def _sanitize_username(name: str) -> str:
    """Coerce an inbound display name into something the webhook API will accept."""
    name = name.strip() or "Unknown User"
    for forbidden in _FORBIDDEN_USERNAME_SUBSTRINGS:
        name = re.sub(re.escape(forbidden), "*" * len(forbidden), name, flags=re.IGNORECASE)
    return name[:_USERNAME_LIMIT]
