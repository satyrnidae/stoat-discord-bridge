"""Stoat sender/receiver services.

Instantiated once per configured Stoat connector (config.yaml's `stoat`
list can have any number of entries - public, self-hosted, or more) since
each Stoat deployment needs its own client/session.
"""

from __future__ import annotations

import json
import urllib.request

import aiohttp

import stoat

from stoat_discord_bridge.admin_commands import ChannelLinker, EmoteLinker, LinkError, StructureMirrorer, UserLinker
from stoat_discord_bridge.channel_structure import ChannelSpec, GuildStructure
from stoat_discord_bridge.config import StoatConnectorConfig
from stoat_discord_bridge.models import (
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

# Stoat message length cap (matches Discord's 2000-char webhook limit; stoat.py
# doesn't expose its own constant, so this mirrors the documented server-side max).
_CONTENT_LIMIT = 2000


def _discover_websocket_base(http_base: str) -> str | None:
    """stoat.Client's `websocket_base` defaults to the public hosted
    instance's gateway (wss://events.stoat.chat/) regardless of `http_base`
    - correct for the public deployment (whose real gateway happens to live
    on that exact domain) but silently wrong for a self-hosted one, which
    then just hangs forever waiting on a response from a server that was
    never going to answer for that token, with no error to show for it.

    Every deployment's REST root reports its actual gateway URL in a `ws`
    field, so fetch that instead of assuming the public one. Best-effort:
    returns None (stoat.Client's own default) on any failure - network
    hiccup, unexpected shape, whatever - rather than blocking startup on it,
    since this is more of an override than something the bridge can't run
    without.
    """
    try:
        with urllib.request.urlopen(http_base.rstrip("/"), timeout=10) as resp:
            data = json.loads(resp.read())
        ws = data.get("ws")
        return ws if isinstance(ws, str) and ws else None
    except Exception:
        return None


class _StoatClient(stoat.Client):
    """stoat.py dispatches events by looking up `on_<event>` attributes on the
    Client instance itself, so *something* has to subclass stoat.Client. This
    subclass exists only to satisfy that and delegates every callback to the
    owning StoatSenderService, which otherwise doesn't need to inherit from a
    third-party client class."""

    def __init__(self, owner: StoatSenderService, config: StoatConnectorConfig) -> None:
        super().__init__(
            token=config.bot_token,
            http_base=config.api_url,
            websocket_base=_discover_websocket_base(config.api_url),
        )
        self._owner = owner

    async def on_ready(self, event, /) -> None:
        await self._owner._handle_ready(event)

    async def on_message(self, message, /) -> None:
        await self._owner._handle_message(message)


class StoatSenderService(SenderService):
    def __init__(
        self,
        config: StoatConnectorConfig,
        on_message: OnMessage,
        health: HealthTracker,
        on_reaction: OnReaction | None = None,
        on_emoji_created: OnEmojiCreated | None = None,
        on_emoji_deleted: OnEmojiDeleted | None = None,
        linker: ChannelLinker | None = None,
        mirrorer: StructureMirrorer | None = None,
        emote_linker: "EmoteLinker | None" = None,
        user_linker: "UserLinker | None" = None,
    ) -> None:
        # linker/mirrorer/emote_linker/user_linker are only needed to serve
        # `/link-channel`, `/mirror-channels`, `/link-emote`, and `/link-user`;
        # None is accepted (e.g. for tests) but those commands will then
        # report themselves unconfigured.
        SenderService.__init__(self, on_message, on_reaction, on_emoji_created, on_emoji_deleted)
        self._config = config
        self.server_id = config.server_id
        self.connector_id = config.id
        self._health = health
        self._linker = linker
        self._mirrorer = mirrorer
        self._emote_linker = emote_linker
        self._user_linker = user_linker
        self._client = _StoatClient(self, config)
        self._self_id: str | None = None

    def get_channel(self, channel_id: str, *, partial: bool = False):
        return self._client.get_channel(channel_id, partial=partial)

    def get_server(self, server_id: str, *, partial: bool = False):
        return self._client.get_server(server_id, partial=partial)

    async def get_channel_name(self, channel_id: str) -> str | None:
        """Best-effort channel-id -> name lookup, used as this connector's
        `ConnectorInfo.resolve_channel_name` for `/link-channel`.

        TODO: verify stoat.py's get_channel(partial=False) semantics - this
        assumes it returns a fully-populated channel object synchronously,
        like partial=True does elsewhere in this class.
        """
        try:
            channel = self._client.get_channel(channel_id, partial=False)
        except Exception:
            return None
        return getattr(channel, "name", None)

    async def ensure_channel(self, name: str) -> str:
        """Idempotent get-or-create by name, for `/mirror-channel`'s
        `ConnectorInfo.ensure_channel` hook - same "match existing by name,
        else create" logic `_mirror_guild_structure` already uses in bulk,
        just for a single channel outside that flow."""
        server = self._client.get_server(self.server_id, partial=True)
        for channel in server.channels:
            if channel.name == name:
                return channel.id
        channel = await server.create_channel(name=name)
        return channel.id

    async def _handle_ready(self, event) -> None:
        self._health.mark_connected(self.connector_id)
        self._self_id = str(event.me.id)
        print(f"[stoat:{self.connector_id}] logged in as {event.me.tag}")

    # stoat.Client has no disconnect/logout-on-drop event to hook (only
    # on_before_connect/on_after_connect for the connect side, and on_logout
    # for an explicit logout) — so connected state here only ever turns on,
    # not off. A dropped connection still shows up as degraded/failing via
    # relay-error tracking in `receive()`.

    async def _handle_message(self, message) -> None:
        if getattr(message.author, "bot", False):
            return
        raw = message.content.strip()
        parts = raw.split()
        cmd = parts[0].lower() if parts else ""
        if cmd == "/status":
            await message.channel.send(self._health.render())
            return
        if cmd == "/mirror-channels":
            await self._handle_mirror_channels(message, parts[1:])
            return
        if cmd == "/link-channel":
            await self._handle_link_channel(message, parts[1:])
            return
        if cmd == "/link-emote":
            await self._handle_link_emote(message, parts[1:])
            return
        if cmd == "/link-user":
            await self._handle_link_user(message, parts[1:])
            return
        if cmd == "/mirror-channel":
            await self._handle_mirror_channel(message, parts[1:])
            return
        avatar_url = getattr(message.author, "avatar_url", None)
        await self._on_message(
            StandardMessage(
                origin_connector_id=self.connector_id,
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
                origin_connector_id=self.connector_id,
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
                origin_connector_id=self.connector_id,
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
        await self._on_emoji_deleted(
            StandardEmojiDeleted(origin_connector_id=self.connector_id, native_id=str(emoji.id))
        )

    async def start(self) -> None:
        # Credentials (token, http_base) are set at construction time above;
        # stoat.Client.start() takes no arguments (unlike discord.Client.start()).
        await self._client.start()

    async def close(self) -> None:
        await self._client.close()

    async def _handle_mirror_channels(self, message, args: list[str], /) -> None:
        """`/mirror-channels <source>`: recreate `<source>`'s (a configured
        Discord connector's) category/channel layout on this Stoat server,
        linking each channel it creates or matches by name back to its
        Discord counterpart. Requires Manage Server so only admins can
        trigger a (potentially large) batch of channel creations.
        """
        if not self._is_admin(message):
            await message.channel.send("You need the Manage Server permission to do that.")
            return
        if not args:
            await message.channel.send("Usage: /mirror-channels <source>")
            return
        source = args[0]

        if self._mirrorer is None:
            await message.channel.send("Mirroring isn't configured.")
            return
        try:
            structure = self._mirrorer.get_structure(source)
        except LinkError as exc:
            await message.channel.send(str(exc))
            return
        except Exception as exc:
            await message.channel.send(f"Couldn't read the '{source}' channel structure: {exc}")
            return

        summary = await _mirror_guild_structure(
            message.channel.server,
            structure,
            source=source,
            local_connector=self.connector_id,
            linker=self._linker,
        )
        await message.channel.send(summary)

    async def _handle_link_channel(self, message, args: list[str], /) -> None:
        if not self._is_admin(message):
            await message.channel.send("You need the Manage Server permission to do that.")
            return
        if len(args) < 2:
            await message.channel.send("Usage: /link-channel <source> <source_id> [<destination_id>]")
            return
        source, source_id, *rest = args
        destination_id = rest[0] if rest else None

        if self._linker is None:
            await message.channel.send("Linking isn't configured.")
            return
        try:
            summary = await self._linker.link_channel(
                local_connector=self.connector_id,
                local_channel_id=str(message.channel.id),
                local_channel_name=getattr(message.channel, "name", str(message.channel.id)),
                source=source,
                source_id=source_id,
                destination_id=destination_id,
            )
        except LinkError as exc:
            await message.channel.send(str(exc))
            return
        await message.channel.send(summary)

    async def _handle_link_emote(self, message, args: list[str], /) -> None:
        if not self._is_admin(message):
            await message.channel.send("You need the Manage Server permission to do that.")
            return
        if len(args) < 3:
            await message.channel.send("Usage: /link-emote <source> <source_id> <local_id>")
            return
        source, source_id, local_id = args[:3]

        if self._emote_linker is None:
            await message.channel.send("Linking isn't configured.")
            return
        try:
            summary = await self._emote_linker.link_emote(
                local_connector=self.connector_id,
                local_id=local_id,
                source=source,
                source_id=source_id,
            )
        except LinkError as exc:
            await message.channel.send(str(exc))
            return
        await message.channel.send(summary)

    async def _handle_link_user(self, message, args: list[str], /) -> None:
        if not self._is_admin(message):
            await message.channel.send("You need the Manage Server permission to do that.")
            return
        if len(args) < 3:
            await message.channel.send("Usage: /link-user <source> <user_id> <local_user_id>")
            return
        source, user_id, local_user_id = args[0], args[1], args[2]

        if self._user_linker is None:
            await message.channel.send("User linking isn't configured.")
            return
        try:
            summary = await self._user_linker.link_user(
                local_connector=self.connector_id,
                local_user_id=local_user_id,
                source=source,
                source_user_id=user_id,
            )
        except LinkError as exc:
            await message.channel.send(str(exc))
            return
        await message.channel.send(summary)

    async def _handle_mirror_channel(self, message, args: list[str], /) -> None:
        if not self._is_admin(message):
            await message.channel.send("You need the Manage Server permission to do that.")
            return
        if not args:
            await message.channel.send("Usage: /mirror-channel <destination|all> [local_channel_id]")
            return
        destination = args[0]
        if len(args) > 1:
            channel_id = channel_name = args[1]  # explicit id - no way to resolve its real display name
        else:
            channel_id = str(message.channel.id)
            channel_name = getattr(message.channel, "name", channel_id)

        if self._linker is None:
            await message.channel.send("Linking isn't configured.")
            return
        try:
            if destination.lower() == "all":
                summary = await self._linker.mirror_channel_all(
                    local_connector=self.connector_id, local_channel_id=channel_id, local_channel_name=channel_name
                )
            else:
                summary = await self._linker.mirror_channel(
                    local_connector=self.connector_id,
                    local_channel_id=channel_id,
                    local_channel_name=channel_name,
                    destination=destination,
                )
        except LinkError as exc:
            await message.channel.send(str(exc))
            return
        await message.channel.send(summary)

    def _is_admin(self, message) -> bool:
        try:
            return bool(message.author_as_member.server_permissions.manage_server)
        except Exception:
            return False


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

    def __init__(self, sender: StoatSenderService, user_mappings: UserMappingRepository | None = None) -> None:
        self.connector_id = sender.connector_id
        self._sender = sender
        self._user_mappings = user_mappings

    async def receive(self, message: StandardMessage, *, target_channel_id: str) -> list[str]:
        channel = self._sender.get_channel(target_channel_id, partial=True)
        masquerade = stoat.MessageMasquerade(
            name=message.sender_name[:32],
            avatar=message.sender_avatar_url,
        )
        content = content_with_attachments(message)
        if self._user_mappings is not None:
            content = await rewrite_mentions(
                content,
                origin_connector_id=message.origin_connector_id,
                target_connector_id=self.connector_id,
                target_kind="stoat",
                user_mappings=self._user_mappings,
            )
        ids: list[str] = []
        for chunk in chunk_content(content, _CONTENT_LIMIT):
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


async def _mirror_guild_structure(
    server: stoat.Server,
    structure: GuildStructure,
    *,
    source: str,
    local_connector: str,
    linker: ChannelLinker | None,
) -> str:
    """Create whatever's missing from `structure` on `server`, then link
    every channel (newly created or already matching by name) back to its
    `source` counterpart.

    Creation is idempotent by name: an existing category/channel is left
    alone rather than duplicated, so a category that already exists never
    has newly added Discord channels folded into it — Stoat has no "add
    channel to category" call, only "create category with these channel
    IDs". Linking, unlike creation, still runs for channels inside an
    already-existing category, so a rerun can link channels an earlier
    run (or manual setup) left unlinked.
    """
    existing_channels: dict[str, str] = {channel.name: channel.id for channel in server.channels}
    existing_group_titles = {category.title for category in server.categories or []}

    created_channels = 0
    skipped_channels = 0
    created_groups = 0
    skipped_groups = 0
    linked_channels = 0
    link_errors: list[str] = []

    async def link(spec: ChannelSpec, local_id: str) -> None:
        nonlocal linked_channels
        if linker is None:
            return
        try:
            await linker.link_channel(
                local_connector=local_connector,
                local_channel_id=str(local_id),
                local_channel_name=spec.name,
                source=source,
                source_id=spec.source_channel_id,
                destination_id=None,
            )
            linked_channels += 1
        except LinkError as exc:
            link_errors.append(f"{spec.name}: {exc}")

    async def process_channels(channels: list[ChannelSpec], *, create_if_missing: bool) -> list[str]:
        nonlocal created_channels, skipped_channels
        ids = []
        for spec in channels:
            existing_id = existing_channels.get(spec.name)
            if existing_id is not None:
                skipped_channels += 1
                await link(spec, existing_id)
                continue
            if not create_if_missing:
                continue  # category already exists - Stoat has no "add channel to category" call
            channel = await server.create_channel(name=spec.name)
            existing_channels[spec.name] = channel.id
            ids.append(channel.id)
            created_channels += 1
            await link(spec, channel.id)
        return ids

    for group in structure.groups:
        if not group.channels:
            skipped_groups += 1
            continue
        category_exists = group.name in existing_group_titles
        channel_ids = await process_channels(group.channels, create_if_missing=not category_exists)
        if category_exists:
            skipped_groups += 1
            continue
        if not channel_ids:
            continue  # every channel in this group already existed elsewhere on the server
        await server.create_category(group.name, channels=channel_ids)
        existing_group_titles.add(group.name)
        created_groups += 1

    await process_channels(structure.ungrouped_channels, create_if_missing=True)

    summary = (
        f"Mirrored '{source}' structure: {created_groups} group(s) and {created_channels} channel(s) created "
        f"({skipped_groups} group(s) and {skipped_channels} channel(s) already existed); "
        f"linked {linked_channels} channel(s)."
    )
    if link_errors:
        shown = link_errors[:5]
        summary += f" {len(link_errors)} link conflict(s): " + "; ".join(shown)
        if len(link_errors) > len(shown):
            summary += f" (+{len(link_errors) - len(shown)} more)"
    return summary
