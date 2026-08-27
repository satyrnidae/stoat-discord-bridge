"""Stoat sender/receiver services.

Instantiated once per configured Stoat connector (config.yaml's `stoat`
list can have any number of entries - public, self-hosted, or more) since
each Stoat deployment needs its own client/session.
"""

from __future__ import annotations

import json
import logging
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

logger = logging.getLogger(__name__)

# Stoat message length cap (matches Discord's 2000-char webhook limit; stoat.py
# doesn't expose its own constant, so this mirrors the documented server-side max).
_CONTENT_LIMIT = 2000

# Discord has native slash-command discoverability; Stoat's commands are
# plain chat messages with no such affordance, hence /bridge-help. See
# COMMANDS.md for full per-command detail - this is a compact pointer to it.
_HELP_TEXT = """Bridge commands (see COMMANDS.md for full detail):
  /status - sync target health, read-only
  /linked-channels - channels bridged to this one, read-only
  /linked-users [user_id] - cross-connector user links, read-only
  /link-channel <source> <source_id> [destination_id] - bridge a channel (Manage Server)
  /link-user <source> <user_id> <local_user_id> - link a user for mentions/masquerading (Manage Server)
  /link-emote <source> <source_id> <local_id> - link a custom emoji (Manage Server)
  /mirror-channel <destination|all> [local_channel_id] - create+link a matching channel (Manage Server)
  /mirror-channels <source> - recreate a Discord guild's structure here (Manage Server)
  /unlink-channel [destination|all] [local_channel_id] - unlink a channel (default: this one) from one connector, or the whole group (Manage Server)
  /unlink-user [destination|all] [user_id] - unlink a user (default: yourself) from one connector, or the whole group (Manage Server)
  /bridge-help - this message"""


def _discover_node_config(http_base: str, *, connector_id: str = "stoat") -> dict | None:
    """Fetches the "NodeInfo"-style config document every stoat.py-compatible
    server exposes at its REST root - used to discover deployment-specific
    URLs that stoat.Client otherwise defaults to the *public* hosted
    instance's for, regardless of `http_base` (see `_discover_websocket_base`
    and `_discover_cdn_base`, both fed from this one fetch rather than each
    hitting the network separately). Best-effort: returns None on any
    failure - network hiccup, unexpected shape, whatever - so callers fall
    back to stoat.Client's own (public-instance) defaults rather than
    blocking startup on it. Unlike that silent fallback, though, the failure
    itself is logged (not swallowed) - a self-hosted deployment silently
    stuck on the public instance's URLs is exactly the failure mode this
    function exists to avoid, so a reverse proxy/WAF rejection, a self-signed
    cert, or a REST root that isn't actually the NodeInfo document all need
    to be visible, not just "avatars never load".

    Sends a real User-Agent (urllib's default, "Python-urllib/x.y", is a
    common bot-blocklist target for reverse proxies/CDNs fronting a
    self-hosted deployment - a 403 for that reason looks identical to a
    genuine network failure without this).
    """
    url = http_base.rstrip("/")
    request = urllib.request.Request(url, headers={"User-Agent": f"stoat-discord-bridge ({connector_id})"})
    try:
        with urllib.request.urlopen(request, timeout=10) as resp:
            body = resp.read()
    except Exception:
        logger.warning(
            "[stoat:%s] couldn't reach '%s' to discover its real websocket/CDN URLs - falling back to the "
            "public instance's, which is wrong for a self-hosted deployment",
            connector_id,
            url,
            exc_info=True,
        )
        return None
    try:
        return json.loads(body)
    except Exception:
        logger.warning(
            "[stoat:%s] '%s' didn't return the expected NodeInfo JSON - falling back to the public instance's "
            "websocket/CDN URLs, which is wrong for a self-hosted deployment; response started with %r",
            connector_id,
            url,
            body[:200],
            exc_info=True,
        )
        return None


def _discover_websocket_base(node_config: dict | None) -> str | None:
    """stoat.Client's `websocket_base` defaults to the public hosted
    instance's gateway (wss://events.stoat.chat/) regardless of `http_base`
    - correct for the public deployment (whose real gateway happens to live
    on that exact domain) but silently wrong for a self-hosted one, which
    then just hangs forever waiting on a response from a server that was
    never going to answer for that token, with no error to show for it.

    Every deployment's REST root reports its actual gateway URL in a `ws`
    field, so use that instead of assuming the public one.
    """
    if node_config is None:
        return None
    ws = node_config.get("ws")
    return ws if isinstance(ws, str) and ws else None


def _discover_cdn_base(node_config: dict | None) -> str | None:
    """stoat.Client's `cdn_base` - which every avatar/attachment/custom-emoji
    URL this bridge builds (via Asset.url()) goes through - defaults to the
    public hosted instance's CDN (`cdn.stoatusercontent.com`, hardcoded in
    stoat.py's CDNClient) regardless of `http_base`, same class of bug as
    `websocket_base` above. For a self-hosted deployment this means every
    asset URL silently points at the wrong server's CDN and never resolves
    - the images just don't exist there - with no error, since URL
    construction itself can't fail.

    Every deployment's REST root reports its actual CDN ("autumn", Revolt's
    - and by extension stoat.py's - name for this microservice) URL at
    features.autumn.url, so use that instead of assuming the public one.
    """
    if node_config is None:
        return None
    try:
        url = node_config["features"]["autumn"]["url"]
    except (KeyError, TypeError):
        return None
    return url if isinstance(url, str) and url else None


class _StoatClient(stoat.Client):
    """stoat.py dispatches events by looking up `on_<event>` attributes on the
    Client instance itself, so *something* has to subclass stoat.Client. This
    subclass exists only to satisfy that and delegates every callback to the
    owning StoatSenderService, which otherwise doesn't need to inherit from a
    third-party client class."""

    def __init__(self, owner: StoatSenderService, config: StoatConnectorConfig) -> None:
        node_config = _discover_node_config(config.api_url, connector_id=config.id)
        super().__init__(
            token=config.bot_token,
            http_base=config.api_url,
            websocket_base=_discover_websocket_base(node_config),
            cdn_base=_discover_cdn_base(node_config),
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

    async def get_user_name(self, user_id: str) -> str | None:
        """Best-effort user-id -> display-name lookup, used as this
        connector's `ConnectorInfo.resolve_user_name` for `/linked-users`."""
        try:
            user = await self._client.fetch_user(user_id)
        except Exception:
            return None
        return getattr(user, "display_name", None) or getattr(user, "tag", None)

    async def get_masquerade_identity(self, user_id: str) -> tuple[str, str | None] | None:
        """Best-effort (display_name, avatar_url) for `user_id` as a member
        of this connector's own Stoat server (`self.server_id` - there's
        exactly one per connector, see StoatConnectorConfig), used by
        StoatReceiverService to masquerade a linked (/link-user) sender as
        their local Stoat identity instead of their remote one. Prefers the
        per-server Member (whose nickname/avatar override applies) over the
        global User, same preference `_resolve_avatar_url` gives a message's
        own author below - deliberately keyed off the connector's own
        server_id rather than derived from a `get_channel(partial=True)`
        object, which - being partial - isn't guaranteed to carry a
        populated server_id at all. Returns None if `user_id` can't be
        resolved to a real name at all (never falls back to displaying the
        bare id - the caller should keep the remote identity instead)."""
        if not self._config.enable_local_user_masquerade:
            logger.debug(
                "[stoat:%s] local user masquerade disabled (enable_local_user_masquerade=false), "
                "not resolving local identity for user %s",
                self.connector_id,
                user_id,
            )
            return None
        try:
            member = await self._client.get_server(self.server_id, partial=True).fetch_member(user_id)
        except Exception as exc:
            logger.debug(
                "[stoat:%s] couldn't fetch server member %s for local user masquerade: %s",
                self.connector_id,
                user_id,
                exc,
            )
            member = None
        # A member's own explicit avatar override (server_avatar, or a
        # global avatar carried on an already-fully-resolved Member) - kept
        # separate from _avatar_url()'s default-avatar fallback below, since
        # that fallback is a generic placeholder, not a real per-user value
        # worth preferring over one fetch_user() will actually resolve.
        member_avatar_override = (
            (getattr(member, "server_avatar", None) or getattr(member, "avatar", None)) if member is not None else None
        )
        name = _display_name(member) if member is not None else ""
        user = None
        if not name:
            # stoat.py's Member.name/display_name properties (confirmed
            # against the installed package, server.py) silently return
            # ""/None rather than the member's real username whenever the
            # Member's `internal_user` reference isn't a locally cached full
            # User object - which a bare fetch_member() result commonly
            # isn't. That's a resolution gap, not evidence the user has no
            # name, so fall back to fetching the User object directly (whose
            # .name is always populated) rather than abandoning local-user
            # masquerade and reverting to the remote identity.
            try:
                user = await self._client.fetch_user(user_id)
            except Exception as exc:
                logger.warning(
                    "[stoat:%s] local user masquerade failed: couldn't resolve linked user %s to a "
                    "server member or a global user: %s",
                    self.connector_id,
                    user_id,
                    exc,
                )
                return None
            name = _display_name(user)
        if not name:
            logger.warning(
                "[stoat:%s] local user masquerade failed: user %s resolved but has no usable display name",
                self.connector_id,
                user_id,
            )
            return None
        # Same fetch_user() fallback for the avatar as for the name above -
        # a Member's own internal_avatar property has the same cache-miss
        # gap, so lean on the explicit override where the member carried
        # one, otherwise resolve from whichever of member/user we actually
        # fetched a usable name from (member's default-avatar fallback only
        # applies when the member itself supplied the name and had no
        # override).
        if member_avatar_override is not None:
            avatar_url = member_avatar_override.url()
        elif user is not None:
            avatar_url = _avatar_url(user)
        else:
            avatar_url = _avatar_url(member)
        logger.debug(
            "[stoat:%s] resolved local user masquerade identity for %s: name=%r avatar_url=%r",
            self.connector_id,
            user_id,
            name,
            avatar_url,
        )
        return name, avatar_url

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
        logger.info("[stoat:%s] logged in as %s", self.connector_id, event.me.tag)

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
        if cmd == "/bridge-help":
            await message.channel.send(_HELP_TEXT)
            return
        if cmd == "/linked-channels":
            await self._handle_linked_channels(message)
            return
        if cmd == "/linked-users":
            await self._handle_linked_users(message, parts[1:])
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
        if cmd == "/unlink-channel":
            await self._handle_unlink_channel(message, parts[1:])
            return
        if cmd == "/unlink-user":
            await self._handle_unlink_user(message, parts[1:])
            return
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
                attachments=[],  # TODO: map stoat.py attachment objects to Attachment once confirmed
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

    # TODO: verify these event names/signatures against stoat.py - modeled on
    # revolt.py's on_reaction_add(message, user_id, emoji_id) /
    # on_reaction_remove(...), which stoat.py's masquerade-based API otherwise
    # closely mirrors.
    async def on_reaction_add(self, message, user_id, emoji_id, /) -> None:
        logger.debug("[stoat:%s] on_reaction_add message=%s user=%s emoji=%s", self.connector_id, message.id, user_id, emoji_id)
        await self._handle_reaction(message, user_id, emoji_id, added=True)

    async def on_reaction_remove(self, message, user_id, emoji_id, /) -> None:
        logger.debug("[stoat:%s] on_reaction_remove message=%s user=%s emoji=%s", self.connector_id, message.id, user_id, emoji_id)
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
        logger.debug("[stoat:%s] on_emoji_create id=%s name=%r", self.connector_id, emoji.id, emoji.name)
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
                    image_url=emoji.image.url(),
                    animated=getattr(emoji, "animated", False),
                ),
            )
        )

    # TODO: verify this event name/payload against stoat.py - guessed
    # symmetric to on_emoji_create above; deletions are never mirrored onto
    # other platforms (see BridgeCoordinator.handle_emoji_deleted), so unlike
    # on_emoji_create there's no self-mirrored-echo to filter out here.
    async def on_emoji_delete(self, emoji, /) -> None:
        logger.debug("[stoat:%s] on_emoji_delete id=%s", self.connector_id, emoji.id)
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

    async def _handle_linked_channels(self, message) -> None:
        if self._linker is None:
            await message.channel.send("Linking isn't configured.")
            return
        summary = await self._linker.list_linked_channels(
            local_connector=self.connector_id, local_channel_id=str(message.channel.id)
        )
        await message.channel.send(summary)

    async def _handle_linked_users(self, message, args: list[str], /) -> None:
        """`/linked-users [local_user_id]`: with no argument, lists every
        cross-connector user link (for debugging); given a Stoat user id,
        shows just that identity's link. No permission gate - read-only,
        same as /status and /linked-channels."""
        if self._user_linker is None:
            await message.channel.send("User linking isn't configured.")
            return
        if args:
            summary = await self._user_linker.list_linked_users(local_connector=self.connector_id, local_user_id=args[0])
        else:
            summary = await self._user_linker.list_linked_users()
        await message.channel.send(summary)

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
        logger.info("[stoat:%s] %s ran /mirror-channels source=%s", self.connector_id, message.author.id, source)
        try:
            structure = self._mirrorer.get_structure(source)
        except LinkError as exc:
            logger.info("[stoat:%s] /mirror-channels rejected: %s", self.connector_id, exc)
            await message.channel.send(str(exc))
            return
        except Exception as exc:
            logger.exception("[stoat:%s] /mirror-channels couldn't read '%s' structure", self.connector_id, source)
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
        logger.info(
            "[stoat:%s] %s ran /link-channel source=%s source_id=%s destination_id=%s",
            self.connector_id,
            message.author.id,
            source,
            source_id,
            destination_id,
        )
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
            logger.info("[stoat:%s] /link-channel rejected: %s", self.connector_id, exc)
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
        logger.info(
            "[stoat:%s] %s ran /link-emote source=%s source_id=%s local_id=%s",
            self.connector_id,
            message.author.id,
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
            logger.info("[stoat:%s] /link-emote rejected: %s", self.connector_id, exc)
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
        logger.info(
            "[stoat:%s] %s ran /link-user source=%s user_id=%s local_user_id=%s",
            self.connector_id,
            message.author.id,
            source,
            user_id,
            local_user_id,
        )
        try:
            summary = await self._user_linker.link_user(
                local_connector=self.connector_id,
                local_user_id=local_user_id,
                source=source,
                source_user_id=user_id,
            )
        except LinkError as exc:
            logger.info("[stoat:%s] /link-user rejected: %s", self.connector_id, exc)
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
        logger.info(
            "[stoat:%s] %s ran /mirror-channel destination=%s local_channel_id=%s",
            self.connector_id,
            message.author.id,
            destination,
            channel_id,
        )
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
            logger.info("[stoat:%s] /mirror-channel rejected: %s", self.connector_id, exc)
            await message.channel.send(str(exc))
            return
        await message.channel.send(summary)

    async def _handle_unlink_channel(self, message, args: list[str], /) -> None:
        """`/unlink-channel [destination|all] [local_channel_id]`: destination
        defaults to "all" (dissolving the whole bridge group);
        local_channel_id defaults to the invoking channel."""
        if not self._is_admin(message):
            await message.channel.send("You need the Manage Server permission to do that.")
            return
        destination = args[0] if args else None
        channel_id = args[1] if len(args) > 1 else str(message.channel.id)

        if self._linker is None:
            await message.channel.send("Linking isn't configured.")
            return
        logger.info(
            "[stoat:%s] %s ran /unlink-channel destination=%s local_channel_id=%s",
            self.connector_id,
            message.author.id,
            destination,
            channel_id,
        )
        try:
            summary = await self._linker.unlink_channel(
                local_connector=self.connector_id, local_channel_id=channel_id, destination=destination
            )
        except LinkError as exc:
            logger.info("[stoat:%s] /unlink-channel rejected: %s", self.connector_id, exc)
            await message.channel.send(str(exc))
            return
        await message.channel.send(summary)

    async def _handle_unlink_user(self, message, args: list[str], /) -> None:
        """`/unlink-user [destination|all] [user_id]`: destination defaults
        to "all" (dissolving the whole link group); user_id defaults to the
        invoking user themselves."""
        if not self._is_admin(message):
            await message.channel.send("You need the Manage Server permission to do that.")
            return
        destination = args[0] if args else None
        user_id = args[1] if len(args) > 1 else str(message.author.id)

        if self._user_linker is None:
            await message.channel.send("User linking isn't configured.")
            return
        logger.info(
            "[stoat:%s] %s ran /unlink-user destination=%s user_id=%s",
            self.connector_id,
            message.author.id,
            destination,
            user_id,
        )
        try:
            summary = await self._user_linker.unlink_user(
                local_connector=self.connector_id, local_user_id=user_id, destination=destination
            )
        except LinkError as exc:
            logger.info("[stoat:%s] /unlink-user rejected: %s", self.connector_id, exc)
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
        sender_name = message.sender_name
        avatar_url = message.sender_avatar_url
        if self._user_mappings is not None:
            local_user_id = await self._user_mappings.find_linked_user_id(
                message.origin_connector_id, message.sender_user_id, self.connector_id
            )
            if local_user_id is not None:
                identity = await self._sender.get_masquerade_identity(local_user_id)
                if identity is not None:
                    sender_name, avatar_url = identity
        masquerade = stoat.MessageMasquerade(
            name=sender_name[:32],
            avatar=avatar_url,
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
            logger.debug(
                "[stoat:%s] sending masqueraded message to channel %s as %r (avatar=%r): %r",
                self.connector_id,
                target_channel_id,
                masquerade.name,
                masquerade.avatar,
                chunk,
            )
            try:
                sent = await channel.send(chunk, masquerade=masquerade)
            except Exception as exc:
                raise PartialRelayError(ids, exc) from exc
            logger.debug("[stoat:%s] masqueraded message sent, id=%s", self.connector_id, sent.id)
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
            server = self._sender.get_server(self._sender.server_id, partial=True)
            image_bytes = await _download(emoji.image_url)
            created = await server.create_emoji(name=emoji.name[:32], image=image_bytes)
        except (stoat.HTTPException, aiohttp.ClientError) as exc:
            logger.warning("[stoat:%s] couldn't create emoji %r: %s", self.connector_id, emoji.name, exc)
            return None  # e.g. emoji slots full, name taken, image too large, network failure - skip this platform
        return CustomEmoji(
            native_id=str(created.id),
            name=created.name,
            image_url=created.image.url(),
            animated=getattr(created, "animated", emoji.animated),
        )


def _display_name(author) -> str:
    """Best-effort display name for a Stoat message author/member.

    stoat.py's `Member.display_name` property - confirmed against the
    installed package (server.py) - passes straight through to the
    underlying User's *account-level* display_name and never reads the
    member's own per-server `nick` field at all, even though `nick` is a
    distinct attribute the same Member carries. Left unchecked, that means a
    member with a server nickname set but no account-level display name
    falls all the way through to `tag` (username#discriminator) - showing a
    raw username where the nickname should appear. So check `nick` first,
    mirroring the same per-server-override-before-global preference already
    given to avatars by `_avatar_url` below.

    Falls back to the bare `name` (not `tag`) when neither is set - a
    masquerade name showing a bare `#0000`-style discriminator suffix reads
    as broken/internal even though it's technically accurate, so it's
    stripped here rather than carried through to whatever's displaying the
    masquerade."""
    return getattr(author, "nick", None) or getattr(author, "display_name", None) or author.name


def _avatar_url(author) -> str | None:
    """Best-effort avatar URL for a Stoat message author. stoat.py exposes
    an avatar as an `Asset` (a `.url()` *method*, not a plain string
    attribute - there's no `avatar_url` shortcut on User/Member, confirmed
    against the installed stoat.py package directly), and a Member's
    optional per-server avatar override takes priority over the
    account-level one when set, matching how it's displayed in the client.
    Falls back to the platform's default avatar if the author has neither."""
    asset = getattr(author, "server_avatar", None) or getattr(author, "avatar", None)
    if asset is not None:
        return asset.url()
    return getattr(author, "default_avatar_url", None)


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
