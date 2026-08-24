"""Discord sender/receiver services.

Sender: a discord.Client that listens for messages in bridged channels and
emits StandardMessages.

Receiver: posts StandardMessages into Discord "as" the originating
Stoat/IRC user, via a per-channel Discord webhook (username + avatar
override) rather than the bridge bot's own identity — so `target_channel_id`
here is a webhook URL, not a Discord channel ID.
"""

from __future__ import annotations

import re

import aiohttp
import discord

from stoat_discord_bridge.channel_structure import ChannelSpec, GroupSpec, GuildStructure, clip_name
from stoat_discord_bridge.config import DiscordConfig
from stoat_discord_bridge.models import (
    Attachment,
    CustomEmoji,
    Platform,
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
from stoat_discord_bridge.status import HealthTracker

# Discord webhook hard limits: 2000 chars per message, 1-80 char usernames,
# and usernames may not contain "clyde" or "discord" (case-insensitive) or
# the API rejects the send outright.
_CONTENT_LIMIT = 2000
_USERNAME_LIMIT = 80
_FORBIDDEN_USERNAME_SUBSTRINGS = ("clyde", "discord")


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

    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent) -> None:
        await self._owner._handle_raw_reaction(payload, added=True)

    async def on_raw_reaction_remove(self, payload: discord.RawReactionActionEvent) -> None:
        await self._owner._handle_raw_reaction(payload, added=False)

    async def on_guild_emojis_update(
        self, guild: discord.Guild, before: "list[discord.Emoji]", after: "list[discord.Emoji]"
    ) -> None:
        await self._owner._handle_guild_emojis_update(guild, before, after)


class DiscordSenderService(SenderService):
    platform = Platform.DISCORD

    def __init__(
        self,
        config: DiscordConfig,
        on_message: OnMessage,
        health: HealthTracker,
        on_reaction: OnReaction | None = None,
        on_emoji_created: OnEmojiCreated | None = None,
        on_emoji_deleted: OnEmojiDeleted | None = None,
    ) -> None:
        SenderService.__init__(self, on_message, on_reaction, on_emoji_created, on_emoji_deleted)
        self._config = config
        self._health = health
        self._commands_synced = False
        self._guild = discord.Object(id=config.guild_id)
        self._client = _DiscordClient(self)
        self.tree = discord.app_commands.CommandTree(self._client)

        @self.tree.command(
            name="status", description="Show sync target health (Discord/Stoat/IRC)", guild=self._guild
        )
        async def status_command(interaction: discord.Interaction) -> None:
            await interaction.response.send_message(self._health.render(), ephemeral=True)

    async def _handle_ready(self) -> None:
        self._health.mark_connected(self.platform)
        if not self._commands_synced:
            await self.tree.sync(guild=self._guild)
            self._commands_synced = True
        print(f"[discord] logged in as {self._client.user} (guild {self._config.guild_id})")

    async def _handle_disconnect(self) -> None:
        self._health.mark_disconnected(self.platform)

    async def _handle_message(self, message: discord.Message) -> None:
        if message.author.bot:
            return
        if message.guild is None or message.guild.id != self._config.guild_id:
            return
        await self._on_message(_to_standard_message(message))

    async def _handle_raw_reaction(self, payload: discord.RawReactionActionEvent, *, added: bool) -> None:
        if self._on_reaction is None or payload.guild_id != self._config.guild_id:
            return
        if payload.user_id == getattr(self._client.user, "id", None):
            return  # the bridge's own mirrored reaction landing back here - drop it, don't re-relay
        if self._is_other_bot(payload):
            return
        await self._on_reaction(_to_standard_reaction(payload, added=added))

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
                        origin_platform=Platform.DISCORD,
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
                    StandardEmojiDeleted(origin_platform=Platform.DISCORD, native_id=str(emoji.id))
                )

    async def start(self) -> None:
        await self._client.start(self._config.bot_token)

    async def close(self) -> None:
        await self._client.close()

    def snapshot_guild_structure(self) -> GuildStructure:
        """Build a platform-neutral snapshot of the bridged guild's current
        categories/channels, for the Stoat `/mirror-channels` command.

        Reads from discord.py's gateway cache (populated by the time
        `on_ready` fires), so this makes no Discord API calls of its own.
        Forum posts are limited to what's currently active in cache —
        archived posts aren't paged in.
        """
        guild = self.get_guild(self._config.guild_id)
        if guild is None:
            raise RuntimeError("Discord guild isn't cached yet — the bridge may still be connecting")

        groups: list[GroupSpec] = []
        for category in guild.categories:
            channels = [
                ChannelSpec(name=clip_name(ch.name))
                for ch in category.channels
                if isinstance(ch, (discord.TextChannel, discord.VoiceChannel))
            ]
            if channels:
                groups.append(GroupSpec(name=clip_name(category.name), channels=channels))

        groups.extend(
            GroupSpec(
                name=clip_name(forum.name),
                channels=[ChannelSpec(name=clip_name(thread.name)) for thread in forum.threads],
            )
            for forum in guild.forums
        )

        ungrouped = [
            ChannelSpec(name=clip_name(ch.name))
            for ch in (*guild.text_channels, *guild.voice_channels)
            if ch.category is None
        ]

        return GuildStructure(groups=groups, ungrouped_channels=ungrouped)


class DiscordReceiverService(ReceiverService):
    platform = Platform.DISCORD
    supports_reactions = True
    supports_emoji = True

    def __init__(self, client: discord.Client, guild_id: int) -> None:
        self._client = client
        self._guild_id = guild_id
        self._session: aiohttp.ClientSession | None = None
        self._webhook_channel_ids: dict[str, int] = {}

    async def receive(self, message: StandardMessage, *, target_channel_id: str) -> list[str]:
        if self._session is None:
            self._session = aiohttp.ClientSession()
        webhook = discord.Webhook.from_url(target_channel_id, session=self._session)
        username = _sanitize_username(message.sender_name)
        ids: list[str] = []
        for chunk in chunk_content(content_with_attachments(message), _CONTENT_LIMIT):
            try:
                sent = await webhook.send(
                    content=chunk,
                    username=username,
                    avatar_url=message.sender_avatar_url,
                    wait=True,
                )
            except Exception as exc:
                raise PartialRelayError(ids, exc) from exc
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
        except (discord.HTTPException, aiohttp.ClientError):
            return None  # emoji slots full, name taken, image too large, etc. - skip this platform
        return CustomEmoji(
            native_id=str(created.id), name=created.name, image_url=str(created.url), animated=created.animated
        )

    async def _get_partial_message(self, target_channel_id: str, target_message_id: str) -> discord.PartialMessage:
        # `target_channel_id` here is a webhook URL (see receive()) - resolve
        # it to a real channel ID once, since reactions need the bot client
        # (webhooks can't react), not the webhook.
        channel_id = self._webhook_channel_ids.get(target_channel_id)
        if channel_id is None:
            if self._session is None:
                self._session = aiohttp.ClientSession()
            webhook = discord.Webhook.from_url(target_channel_id, session=self._session)
            fetched = await webhook.fetch()
            channel_id = fetched.channel_id
            self._webhook_channel_ids[target_channel_id] = channel_id
        channel = self._client.get_channel(channel_id) or await self._client.fetch_channel(channel_id)
        return channel.get_partial_message(int(target_message_id))

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None


def _to_standard_message(message: discord.Message) -> StandardMessage:
    return StandardMessage(
        origin_platform=Platform.DISCORD,
        origin_channel_id=str(message.channel.id),
        channel_name=getattr(message.channel, "name", str(message.channel.id)),
        sender_name=message.author.display_name,
        sender_avatar_url=str(message.author.display_avatar.url) if message.author.display_avatar else None,
        content_markdown=message.content,
        message_id=str(message.id),
        attachments=[
            Attachment(url=a.url, filename=a.filename, content_type=a.content_type, size_bytes=a.size)
            for a in message.attachments
        ],
    )


def _to_standard_reaction(payload: discord.RawReactionActionEvent, *, added: bool) -> StandardReaction:
    emoji = payload.emoji
    emoji_repr: str | CustomEmoji
    if emoji.is_custom_emoji():
        emoji_repr = CustomEmoji(
            native_id=str(emoji.id), name=emoji.name or "", image_url=str(emoji.url), animated=emoji.animated
        )
    else:
        emoji_repr = emoji.name  # plain unicode emoji
    return StandardReaction(
        origin_platform=Platform.DISCORD,
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
