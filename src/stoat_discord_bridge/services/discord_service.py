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

import aiohttp
import discord
from discord import app_commands

from stoat_discord_bridge.admin_commands import ChannelLinker, ConnectorInfo, EmoteLinker, LinkError, UserLinker
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
    OnMessage,
    OnReaction,
    PartialRelayError,
    ReceiverService,
    SenderService,
)
from stoat_discord_bridge.services.formatting import (
    chunk_content,
    content_with_attachments,
)
from stoat_discord_bridge.services.mentions import rewrite_mentions
from stoat_discord_bridge.status import HealthTracker
from stoat_discord_bridge.storage.user_mappings import UserMappingRepository

logger = logging.getLogger(__name__)

# Discord webhook hard limits: 2000 chars per message, 1-80 char usernames,
# and usernames may not contain "clyde" or "discord" (case-insensitive) or
# the API rejects the send outright.
_CONTENT_LIMIT = 2000
_USERNAME_LIMIT = 80
_FORBIDDEN_USERNAME_SUBSTRINGS = ("clyde", "discord")

_CHANNEL_MENTION_RE = re.compile(r"^<#(\d+)>$")


def _connector_autocomplete_choices(
    current: str, connectors: dict[str, ConnectorInfo], *, include_all: bool = False
) -> list[app_commands.Choice[str]]:
    """Shared filtering behind every `source`/`destination` option's
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
    """The `source_id`/`destination_id`/`local_channel_id` slash-command
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
    ) -> None:
        # linker/emote_linker/user_linker are only needed to serve
        # `/link-channel`/`/link-emote`/`/link-user`; None is accepted (e.g.
        # for tests) but those commands will then report themselves unconfigured.
        SenderService.__init__(self, on_message, on_reaction, on_emoji_created, on_emoji_deleted)
        self._config = config
        self.connector_id = config.id
        self._health = health
        self._linker = linker
        self._emote_linker = emote_linker
        self._user_linker = user_linker
        self._commands_synced = False
        # Discord thread ids whose auto-mirror (_handle_thread_create) has
        # fired but whose starter message hasn't arrived through
        # _handle_message yet - see both methods' docstrings.
        self._pending_thread_intro: set[int] = set()
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
            name="linked-users",
            description="List cross-connector user links, for debugging - or just one member's, if given",
            guild=self._guild,
        )
        @app_commands.describe(user="Show only this member's link (omit to list every linked user)")
        async def linked_users_command(
            interaction: discord.Interaction, user: discord.Member | None = None
        ) -> None:
            await self._handle_linked_users(interaction, user)

        async def link_channel_source_autocomplete(
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
            source="Connector id to link from (see /status for configured connectors)",
            source_id="Channel id on that connector",
            destination_id="Channel id on this connector (defaults to the current channel)",
        )
        @app_commands.autocomplete(source=link_channel_source_autocomplete)
        async def link_channel_command(
            interaction: discord.Interaction, source: str, source_id: str, destination_id: str | None = None
        ) -> None:
            await self._handle_link_channel(interaction, source, source_id, destination_id)

        async def link_emote_source_autocomplete(
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
            source="Connector id to link from (see /status for configured connectors)",
            source_id="Emoji id on that connector",
            local_id="Emoji id on this connector",
        )
        @app_commands.autocomplete(source=link_emote_source_autocomplete)
        async def link_emote_command(
            interaction: discord.Interaction, source: str, source_id: str, local_id: str
        ) -> None:
            await self._handle_link_emote(interaction, source, source_id, local_id)

        async def link_user_source_autocomplete(
            interaction: discord.Interaction, current: str
        ) -> list[app_commands.Choice[str]]:
            connectors = self._user_linker.connectors if self._user_linker is not None else {}
            return _connector_autocomplete_choices(current, connectors)

        @self.tree.command(
            name="link-user",
            description="Link a user from another connector to a local user, for mention rewriting and masquerade override",
            guild=self._guild,
        )
        @app_commands.default_permissions(manage_guild=True)
        @app_commands.describe(
            source="Connector id to link from (see /status for configured connectors)",
            user_id="User id on that connector",
            local_user="The Discord member this is the same person as",
        )
        @app_commands.autocomplete(source=link_user_source_autocomplete)
        async def link_user_command(
            interaction: discord.Interaction, source: str, user_id: str, local_user: discord.Member
        ) -> None:
            await self._handle_link_user(interaction, source, user_id, local_user)

        async def mirror_channel_destination_autocomplete(
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
            destination="Connector id to mirror to, or 'all' for every configured connector",
            local_channel_id="Channel id on this connector (defaults to the current channel)",
        )
        @app_commands.autocomplete(destination=mirror_channel_destination_autocomplete)
        async def mirror_channel_command(
            interaction: discord.Interaction, destination: str, local_channel_id: str | None = None
        ) -> None:
            await self._handle_mirror_channel(interaction, destination, local_channel_id)

        async def unlink_channel_destination_autocomplete(
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
            destination="Connector id to unlink, or 'all' to dissolve the whole bridge group (default: all)",
            local_channel_id="Channel id on this connector (defaults to the current channel)",
        )
        @app_commands.autocomplete(destination=unlink_channel_destination_autocomplete)
        async def unlink_channel_command(
            interaction: discord.Interaction, destination: str | None = None, local_channel_id: str | None = None
        ) -> None:
            await self._handle_unlink_channel(interaction, destination, local_channel_id)

        async def unlink_user_destination_autocomplete(
            interaction: discord.Interaction, current: str
        ) -> list[app_commands.Choice[str]]:
            connectors = self._user_linker.connectors if self._user_linker is not None else {}
            return _connector_autocomplete_choices(current, connectors, include_all=True)

        @self.tree.command(
            name="unlink-user",
            description="Unlink a user's cross-connector identity - one connector, or the whole group (default: all)",
            guild=self._guild,
        )
        @app_commands.default_permissions(manage_guild=True)
        @app_commands.describe(
            destination="Connector id to unlink, or 'all' to dissolve the whole link group (default: all)",
            user="Member to unlink (defaults to yourself)",
        )
        @app_commands.autocomplete(destination=unlink_user_destination_autocomplete)
        async def unlink_user_command(
            interaction: discord.Interaction, destination: str | None = None, user: discord.Member | None = None
        ) -> None:
            await self._handle_unlink_user(interaction, destination, user)

    @property
    def client(self) -> discord.Client:
        return self._client

    async def _handle_ready(self) -> None:
        self._health.mark_connected(self.connector_id)
        if not self._commands_synced:
            await self.tree.sync(guild=self._guild)
            self._commands_synced = True
            logger.debug("[discord:%s] slash commands synced", self.connector_id)
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
        if message.channel.id in self._pending_thread_intro:
            # The starter message of a thread _handle_thread_create just
            # auto-mirrored - it already announced the new channel, so drop
            # this one instead of relaying its raw content (usually just the
            # thread name again) into the freshly-linked channels.
            self._pending_thread_intro.discard(message.channel.id)
            return
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
        exists and is linked on every other connector, then announce that in
        the newly-linked channels rather than the raw starter message
        (suppressed via _pending_thread_intro - see _handle_message). Only
        fires for a thread whose parent channel is itself already bridged,
        so this doesn't auto-mirror every thread created anywhere in the
        guild. One-way (Discord -> Stoat/IRC) only; Stoat/IRC have no
        equivalent "created a thread" event of their own yet.

        `thread.id` is recorded *before* any `await` below so _handle_message
        is guaranteed to see it in time for the thread's own starter
        message: discord.py dispatches gateway events (and so schedules each
        handler's task) in the order they're received, and THREAD_CREATE
        always precedes the MESSAGE_CREATE for a new thread's first message.

        The mirrored channel is placed into a Category named after the
        thread's *parent channel* - not any real Discord Category the parent
        itself belongs to - so every thread/forum-post under the same parent
        groups together on the destination, deliberately overriding the
        general "mirror the source's own Category" rule /mirror-channel
        otherwise follows.
        """
        if self._linker is None or thread.guild.id != self._config.guild_id:
            return
        parent = thread.parent
        if parent is None or not await self._linker.is_linked(self.connector_id, str(parent.id)):
            return  # this thread's parent was never bridged - leave the thread alone

        self._pending_thread_intro.add(thread.id)
        try:
            await self._linker.mirror_channel_all(
                local_connector=self.connector_id,
                local_channel_id=str(thread.id),
                local_channel_name=clip_name(thread.name),
                local_channel_category=clip_name(parent.name),
            )
        except Exception:
            logger.exception("[discord:%s] failed to auto-mirror thread %s", self.connector_id, thread.id)
            self._pending_thread_intro.discard(thread.id)
            return

        bot_user = self._client.user
        link = f"https://discord.com/channels/{thread.guild.id}/{thread.id}"
        await self._on_message(
            StandardMessage(
                origin_connector_id=self.connector_id,
                origin_channel_id=str(thread.id),
                channel_name=thread.name,
                sender_name=bot_user.display_name if bot_user is not None else "Bridge",
                sender_avatar_url=(
                    str(bot_user.display_avatar.url) if bot_user is not None and bot_user.display_avatar else None
                ),
                sender_user_id=str(bot_user.id) if bot_user is not None else "",
                content_markdown=f"Created a new channel {link}",
                message_id=f"thread-created-{thread.id}",
            )
        )

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
        /mirror-channel's local_channel_id option - can fail in ways short
        of a clean discord.py exception (fetch_channel(), unlike
        get_channel_name's other call sites, isn't otherwise guarded here)."""
        try:
            channel = self._client.get_channel(int(channel_id)) or await self._client.fetch_channel(int(channel_id))
            category = getattr(channel, "category", None)
        except Exception:
            logger.debug("[discord:%s] couldn't resolve channel category for %s", self.connector_id, channel_id)
            return None
        return category.name if category is not None else None

    async def get_user_name(self, user_id: str) -> str | None:
        """Best-effort user-id -> display-name lookup, used as this
        connector's `ConnectorInfo.resolve_user_name` for `/linked-users`."""
        try:
            user = self._client.get_user(int(user_id)) or await self._client.fetch_user(int(user_id))
        except (discord.HTTPException, discord.NotFound, ValueError):
            logger.debug("[discord:%s] couldn't resolve user name for %s", self.connector_id, user_id)
            return None
        return getattr(user, "display_name", None)

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

    async def _handle_linked_users(self, interaction: discord.Interaction, user: discord.Member | None) -> None:
        if self._user_linker is None:
            await interaction.response.send_message("User linking isn't configured.", ephemeral=True)
            return
        if user is not None:
            summary = await self._user_linker.list_linked_users(
                local_connector=self.connector_id, local_user_id=str(user.id)
            )
        else:
            summary = await self._user_linker.list_linked_users()
        await interaction.response.send_message(summary, ephemeral=True)

    async def _handle_link_channel(
        self, interaction: discord.Interaction, source: str, source_id: str, destination_id: str | None
    ) -> None:
        if self._linker is None:
            await interaction.response.send_message("Linking isn't configured.", ephemeral=True)
            return
        source_id = _normalize_channel_id(source_id)
        if destination_id is not None:
            destination_id = _normalize_channel_id(destination_id)
        logger.info(
            "[discord:%s] %s ran /link-channel source=%s source_id=%s destination_id=%s",
            self.connector_id,
            interaction.user.id,
            source,
            source_id,
            destination_id,
        )
        try:
            summary = await self._linker.link_channel(
                local_connector=self.connector_id,
                local_channel_id=str(interaction.channel_id),
                local_channel_name=getattr(interaction.channel, "name", str(interaction.channel_id)),
                source=source,
                source_id=source_id,
                destination_id=destination_id,
            )
        except LinkError as exc:
            logger.info("[discord:%s] /link-channel rejected: %s", self.connector_id, exc)
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        await interaction.response.send_message(summary, ephemeral=True)

    async def _handle_link_emote(
        self, interaction: discord.Interaction, source: str, source_id: str, local_id: str
    ) -> None:
        if self._emote_linker is None:
            await interaction.response.send_message("Linking isn't configured.", ephemeral=True)
            return
        logger.info(
            "[discord:%s] %s ran /link-emote source=%s source_id=%s local_id=%s",
            self.connector_id,
            interaction.user.id,
            source,
            source_id,
            local_id,
        )
        try:
            summary = await self._emote_linker.link_emote(
                local_connector=self.connector_id,
                local_id=local_id,
                source=source,
                source_id=source_id,
            )
        except LinkError as exc:
            logger.info("[discord:%s] /link-emote rejected: %s", self.connector_id, exc)
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        await interaction.response.send_message(summary, ephemeral=True)

    async def _handle_link_user(
        self, interaction: discord.Interaction, source: str, user_id: str, local_user: discord.Member
    ) -> None:
        # local_user is a real discord.Member (picked from Discord's own
        # member search, not typed as free text) specifically so this can't
        # end up linked to a mistyped/malformed id or a bare "@name" - see
        # LinkError-free "Unknown User"/`<@@name>` mangling that caused
        # further downstream once such a bad id was already on file.
        if self._user_linker is None:
            await interaction.response.send_message("User linking isn't configured.", ephemeral=True)
            return
        logger.info(
            "[discord:%s] %s ran /link-user source=%s user_id=%s local_user=%s",
            self.connector_id,
            interaction.user.id,
            source,
            user_id,
            local_user.id,
        )
        try:
            summary = await self._user_linker.link_user(
                local_connector=self.connector_id,
                local_user_id=str(local_user.id),
                source=source,
                source_user_id=user_id,
            )
        except LinkError as exc:
            logger.info("[discord:%s] /link-user rejected: %s", self.connector_id, exc)
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        await interaction.response.send_message(summary, ephemeral=True)

    async def _handle_mirror_channel(
        self, interaction: discord.Interaction, destination: str, local_channel_id: str | None
    ) -> None:
        if self._linker is None:
            await interaction.response.send_message("Linking isn't configured.", ephemeral=True)
            return
        if local_channel_id is not None:
            channel_id = _normalize_channel_id(local_channel_id)
            channel_name = await self.get_channel_name(channel_id) or channel_id
        else:
            channel_id = str(interaction.channel_id)
            channel_name = getattr(interaction.channel, "name", channel_id)
        channel_category = await self.get_channel_category_name(channel_id)
        logger.info(
            "[discord:%s] %s ran /mirror-channel destination=%s local_channel_id=%s",
            self.connector_id,
            interaction.user.id,
            destination,
            channel_id,
        )
        try:
            if destination.lower() == "all":
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
                    destination=destination,
                    local_channel_category=channel_category,
                )
        except LinkError as exc:
            logger.info("[discord:%s] /mirror-channel rejected: %s", self.connector_id, exc)
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        await interaction.response.send_message(summary, ephemeral=True)

    async def _handle_unlink_channel(
        self, interaction: discord.Interaction, destination: str | None, local_channel_id: str | None
    ) -> None:
        if self._linker is None:
            await interaction.response.send_message("Linking isn't configured.", ephemeral=True)
            return
        channel_id = _normalize_channel_id(local_channel_id) if local_channel_id is not None else str(interaction.channel_id)
        logger.info(
            "[discord:%s] %s ran /unlink-channel destination=%s local_channel_id=%s",
            self.connector_id,
            interaction.user.id,
            destination,
            channel_id,
        )
        try:
            summary = await self._linker.unlink_channel(
                local_connector=self.connector_id, local_channel_id=channel_id, destination=destination
            )
        except LinkError as exc:
            logger.info("[discord:%s] /unlink-channel rejected: %s", self.connector_id, exc)
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        await interaction.response.send_message(summary, ephemeral=True)

    async def _handle_unlink_user(
        self, interaction: discord.Interaction, destination: str | None, user: discord.Member | None
    ) -> None:
        if self._user_linker is None:
            await interaction.response.send_message("User linking isn't configured.", ephemeral=True)
            return
        target = user or interaction.user
        logger.info(
            "[discord:%s] %s ran /unlink-user destination=%s user=%s",
            self.connector_id,
            interaction.user.id,
            destination,
            target.id,
        )
        try:
            summary = await self._user_linker.unlink_user(
                local_connector=self.connector_id, local_user_id=str(target.id), destination=destination
            )
        except LinkError as exc:
            logger.info("[discord:%s] /unlink-user rejected: %s", self.connector_id, exc)
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
    ) -> None:
        self._client = client
        self._guild_id = guild_id
        self.connector_id = connector_id
        self._user_mappings = user_mappings
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
