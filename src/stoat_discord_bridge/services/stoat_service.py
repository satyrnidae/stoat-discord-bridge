"""Stoat sender/receiver services.

Instantiated twice by the bridge — once per StoatServerConfig (public,
self-hosted) — since each Stoat deployment needs its own client/session.
"""

from __future__ import annotations

import aiohttp
from collections.abc import Callable

import stoat

from stoat_discord_bridge.channel_structure import ChannelSpec, GuildStructure
from stoat_discord_bridge.config import StoatServerConfig
from stoat_discord_bridge.models import (
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

# Stoat message length cap (matches Discord's 2000-char webhook limit; stoat.py
# doesn't expose its own constant, so this mirrors the documented server-side max).
_CONTENT_LIMIT = 2000


class _StoatClient(stoat.Client):
    """stoat.py dispatches events by looking up `on_<event>` attributes on the
    Client instance itself, so *something* has to subclass stoat.Client. This
    subclass exists only to satisfy that and delegates every callback to the
    owning StoatSenderService, which otherwise doesn't need to inherit from a
    third-party client class."""

    def __init__(self, owner: StoatSenderService, config: StoatServerConfig) -> None:
        super().__init__(token=config.bot_token, http_base=config.api_url)
        self._owner = owner

    async def on_ready(self, event, /) -> None:
        await self._owner._handle_ready(event)

    async def on_message(self, message, /) -> None:
        await self._owner._handle_message(message)


class StoatSenderService(SenderService):
    def __init__(
        self,
        config: StoatServerConfig,
        platform: Platform,
        on_message: OnMessage,
        health: HealthTracker,
        on_reaction: OnReaction | None = None,
        on_emoji_created: OnEmojiCreated | None = None,
        on_emoji_deleted: OnEmojiDeleted | None = None,
        guild_structure_provider: Callable[[], GuildStructure] | None = None,
    ) -> None:
        # guild_structure_provider is only needed to serve `/mirror-channels`;
        # None is accepted (e.g. for tests) but that command will then error.
        SenderService.__init__(self, on_message, on_reaction, on_emoji_created, on_emoji_deleted)
        self._config = config
        self.server_id = config.server_id
        self.platform = platform
        self._health = health
        self._client = _StoatClient(self, config)
        self._self_id: str | None = None
        self._guild_structure_provider = guild_structure_provider

    def get_channel(self, channel_id: str, *, partial: bool = False):
        return self._client.get_channel(channel_id, partial=partial)

    async def _handle_ready(self, event) -> None:
        self._health.mark_connected(self.platform)
        self._self_id = str(event.me.id)
        print(f"[stoat:{self._config.server_id}] logged in as {event.me.tag}")

    # stoat.Client has no disconnect/logout-on-drop event to hook (only
    # on_before_connect/on_after_connect for the connect side, and on_logout
    # for an explicit logout) — so connected state here only ever turns on,
    # not off. A dropped connection still shows up as degraded/failing via
    # relay-error tracking in `receive()`.

    async def _handle_message(self, message) -> None:
        if getattr(message.author, "bot", False):
            return
        content = message.content.strip().lower()
        if content == "/status":
            await message.channel.send(self._health.render())
            return
        if content == "/mirror-channels":
            await self._handle_mirror_channels(message)
            return
        avatar_url = getattr(message.author, "avatar_url", None)
        await self._on_message(
            StandardMessage(
                origin_platform=self.platform,
                origin_channel_id=str(message.channel.id),
                channel_name=getattr(message.channel, "name", str(message.channel.id)),
                sender_name=getattr(message.author, "display_name", None) or message.author.tag,
                sender_avatar_url=str(avatar_url) if avatar_url is not None else None,
                content_markdown=message.content,
                message_id=str(message.id),
                attachments=[],  # TODO: map stoat.py attachment objects to Attachment once confirmed
            )
        )

    # TODO: verify these event names/signatures against stoat.py - modeled on
    # revolt.py's on_reaction_add(message, user_id, emoji_id) /
    # on_reaction_remove(...), which stoat.py's masquerade-based API otherwise
    # closely mirrors.
    async def on_reaction_add(self, message, user_id, emoji_id, /) -> None:
        await self._handle_reaction(message, user_id, emoji_id, added=True)

    async def on_reaction_remove(self, message, user_id, emoji_id, /) -> None:
        await self._handle_reaction(message, user_id, emoji_id, added=False)

    async def _handle_reaction(self, message, user_id, emoji_id, *, added: bool) -> None:
        if self._on_reaction is None or str(user_id) == self._self_id:
            return  # the bridge's own mirrored reaction landing back here - drop it, don't re-relay
        await self._on_reaction(
            StandardReaction(
                origin_platform=self.platform,
                origin_channel_id=str(message.channel.id),
                origin_message_id=str(message.id),
                emoji=_parse_stoat_emoji(emoji_id),
                added=added,
            )
        )

    # TODO: verify this event name/payload against stoat.py - no confirmed
    # equivalent yet; server-level emoji creation may instead surface via a
    # generic on_server_update and require diffing `server.emojis`.
    async def on_emoji_create(self, emoji, /) -> None:
        if self._on_emoji_created is None:
            return
        if getattr(emoji, "creator_id", None) is not None and str(emoji.creator_id) == self._self_id:
            return  # the bridge's own mirrored emoji landing back here - drop it, don't re-mirror
        await self._on_emoji_created(
            StandardEmojiCreated(
                origin_platform=self.platform,
                emoji=CustomEmoji(
                    native_id=str(emoji.id),
                    name=emoji.name,
                    image_url=emoji.image_url if hasattr(emoji, "image_url") else str(emoji.url),
                    animated=getattr(emoji, "animated", False),
                ),
            )
        )

    # TODO: verify this event name/payload against stoat.py - guessed
    # symmetric to on_emoji_create above; deletions are never mirrored onto
    # other platforms (see BridgeCoordinator.handle_emoji_deleted), so unlike
    # on_emoji_create there's no self-mirrored-echo to filter out here.
    async def on_emoji_delete(self, emoji, /) -> None:
        if self._on_emoji_deleted is None:
            return
        await self._on_emoji_deleted(StandardEmojiDeleted(origin_platform=self.platform, native_id=str(emoji.id)))

    async def start(self) -> None:
        # Credentials (token, http_base) are set at construction time above;
        # stoat.Client.start() takes no arguments (unlike discord.Client.start()).
        await self._client.start()

    async def close(self) -> None:
        await self._client.close()

    async def _handle_mirror_channels(self, message, /) -> None:
        """`/mirror-channels`: recreate the bridged Discord guild's category/
        channel layout on this Stoat server. Requires Manage Server so only
        admins can trigger a (potentially large) batch of channel creations.
        """
        try:
            is_admin = message.author_as_member.server_permissions.manage_server
        except Exception:
            is_admin = False
        if not is_admin:
            await message.channel.send("You need the Manage Server permission to do that.")
            return

        try:
            structure = self._guild_structure_provider()
        except Exception as exc:
            await message.channel.send(f"Couldn't read the Discord channel structure: {exc}")
            return

        summary = await _mirror_guild_structure(message.channel.server, structure)
        await message.channel.send(summary)


class StoatReceiverService(ReceiverService):
    """Posts into Stoat "as" a remote (Discord/IRC) user via masquerade.

    Masquerade is a `send()` kwarg (`MessageMasquerade(name=, avatar=)`), not
    a separate webhook-style API, so this reuses the already-connected
    sender client for the same server rather than needing its own identity
    to post through. The bot must have the `use_masquerade` permission in
    the target channel.
    """

    supports_reactions = True
    supports_emoji = True

    def __init__(self, sender: StoatSenderService) -> None:
        self.platform = sender.platform
        self._sender = sender

    async def receive(self, message: StandardMessage, *, target_channel_id: str) -> list[str]:
        channel = self._sender.get_channel(target_channel_id, partial=True)
        masquerade = stoat.MessageMasquerade(
            name=message.sender_name[:32],
            avatar=message.sender_avatar_url,
        )
        ids: list[str] = []
        for chunk in chunk_content(content_with_attachments(message), _CONTENT_LIMIT):
            try:
                sent = await channel.send(chunk, masquerade=masquerade)
            except Exception as exc:
                raise PartialRelayError(ids, exc) from exc
            ids.append(str(sent.id))
        return ids

    async def add_reaction(self, *, target_channel_id: str, target_message_id: str, emoji: str | CustomEmoji) -> None:
        message = self._sender.get_channel(target_channel_id, partial=True).get_message(
            target_message_id, partial=True
        )
        await message.add_reaction(_to_stoat_emoji(emoji))

    async def remove_reaction(
        self, *, target_channel_id: str, target_message_id: str, emoji: str | CustomEmoji
    ) -> None:
        message = self._sender.get_channel(target_channel_id, partial=True).get_message(
            target_message_id, partial=True
        )
        # TODO: verify against stoat.py directly (not installable to check).
        # revolt.py - the closest real analog, same masquerade-based API -
        # has remove_reaction(emoji, user=None, remove_all=False), which
        # without a user hits DELETE .../reactions/{emoji}?remove_all=false
        # with no user_id: per the Revolt API that removes only the calling
        # (bot's) own reaction, not every user's - so this call should be safe
        # as-is if stoat.py matches revolt.py's default here.
        await message.remove_reaction(_to_stoat_emoji(emoji))

    async def create_emoji(self, emoji: CustomEmoji) -> CustomEmoji | None:
        try:
            # TODO: verify how stoat.Client exposes the connected server object -
            # guessing a `.get_server(id, partial=True)` accessor mirroring
            # `.get_channel(id, partial=True)`, used elsewhere in this class.
            server = self._sender.get_server(self._sender.server_id, partial=True)
            image_bytes = await _download(emoji.image_url)
            created = await server.create_emoji(name=emoji.name[:32], image=image_bytes)
        except (stoat.HTTPException, aiohttp.ClientError):
            return None  # e.g. emoji slots full, name taken, image too large, network failure - skip this platform
        return CustomEmoji(
            native_id=str(created.id),
            name=created.name,
            image_url=created.image_url if hasattr(created, "image_url") else str(created.url),
            animated=getattr(created, "animated", emoji.animated),
        )


async def _download(url: str) -> bytes:
    async with aiohttp.ClientSession() as session, session.get(url) as resp:
        resp.raise_for_status()
        return await resp.read()


def _parse_stoat_emoji(emoji_id: str) -> str | CustomEmoji:
    # TODO: verify how stoat.py distinguishes a unicode emoji from a custom
    # emoji ID in reaction events - assumed here to mirror revolt.py, where
    # unicode reactions carry the literal emoji character as the "ID" and
    # custom emoji carry their (26-char, base32) ULID. isalnum() alone is
    # the distinguishing check: a real unicode emoji - single codepoint,
    # flag, ZWJ sequence, skin-tone modifier, whatever its length - is never
    # alnum, while a ULID always is, so no length check is needed (and an
    # earlier `len(emoji_id) > 8` guard here was wrong: every real 26-char
    # ULID is longer than 8, so it made the CUSTOM branch unreachable).
    if not emoji_id.isalnum():
        return emoji_id  # unicode emoji, passed straight through
    return CustomEmoji(native_id=emoji_id, name="", image_url="")


def _to_stoat_emoji(emoji: str | CustomEmoji) -> str:
    return emoji if isinstance(emoji, str) else emoji.native_id


async def _mirror_guild_structure(server: stoat.Server, structure: GuildStructure) -> str:
    """Create whatever's missing from `structure` on `server`.

    Idempotent by name: an existing category/channel is left alone rather
    than duplicated, so a category that already exists never has newly
    added Discord channels folded into it — Stoat has no "add channel to
    category" call, only "create category with these channel IDs".
    """
    existing_channel_names = {channel.name for channel in server.channels}
    existing_group_titles = {category.title for category in server.categories or []}

    created_channels = 0
    skipped_channels = 0
    created_groups = 0
    skipped_groups = 0

    async def create_missing(channels: list[ChannelSpec]) -> list[str]:
        nonlocal created_channels, skipped_channels
        ids = []
        for spec in channels:
            if spec.name in existing_channel_names:
                skipped_channels += 1
                continue
            channel = await server.create_channel(name=spec.name)
            existing_channel_names.add(spec.name)
            ids.append(channel.id)
            created_channels += 1
        return ids

    for group in structure.groups:
        if group.name in existing_group_titles or not group.channels:
            skipped_groups += 1
            continue
        channel_ids = await create_missing(group.channels)
        if not channel_ids:
            continue  # every channel in this group already existed elsewhere on the server
        await server.create_category(group.name, channels=channel_ids)
        existing_group_titles.add(group.name)
        created_groups += 1

    await create_missing(structure.ungrouped_channels)

    return (
        f"Mirrored Discord structure: {created_groups} group(s) and {created_channels} channel(s) created "
        f"({skipped_groups} group(s) and {skipped_channels} channel(s) already existed)."
    )
