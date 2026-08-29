"""Stoat sender/receiver services.

Instantiated once per configured Stoat connector (config.yaml's `stoat`
list can have any number of entries - public, self-hosted, or more) since
each Stoat deployment needs its own client/session.
"""

from __future__ import annotations

import json
import logging
import urllib.request
from types import SimpleNamespace

import aiohttp

import stoat
from stoat import routes as stoat_routes
from stoat.core import ulid_new

from stoat_discord_bridge.admin_commands import (
    CategoryLinker,
    ChannelLinker,
    EmoteLinker,
    LinkError,
    RoleLinker,
    StructureMirrorer,
    UserLinker,
)
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
    OnMemberRolesChanged,
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
from stoat_discord_bridge.services.mentions import (
    rewrite_channel_mentions,
    rewrite_mentions,
    rewrite_role_mentions,
)
from stoat_discord_bridge.status import HealthTracker
from stoat_discord_bridge.storage.channel_mappings import ChannelMappingRepository
from stoat_discord_bridge.storage.role_mappings import RoleMappingRepository
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
  /linked-categories - Categories bridged to this channel's Category, read-only
  /linked-users [local_id] - cross-connector user links, read-only
  /link-channel <service> <external_id> [local_id] - bridge a channel (Manage Server)
  /link-category <service> <external_id> [local_id] - bridge a Category; new channels in either sync automatically (Manage Server)
  /link-user <service> <external_id> <local_id> - link a user for mentions/masquerading (Manage Server)
  /link-emote <service> <external_id> <local_id> - link a custom emoji (Manage Server)
  /mirror-channel <service|all> [local_id] - create+link a matching channel (Manage Server)
  /mirror-channels <service> - recreate a Discord guild's structure here (Manage Server)
  /unlink-channel [service|all] [local_id] - unlink a channel (default: this one) from one connector, or the whole group (Manage Server)
  /unlink-category [service|all] - unlink this channel's Category (default: whole group) from one connector, or the whole group (Manage Server)
  /unlink-user [service|all] [local_id] - unlink a user (default: yourself) from one connector, or the whole group (Manage Server)
  /link role <local_id|name> <service> <external_id|name> - link a role across connectors (Manage Server)
  /mirror role <local_id|name> [service|all] - create+link a matching role on another connector (Manage Server)
  /linked roles [local_id|name] - roles linked across the bridge, read-only
  /unlink role <local_id|name> [service|all] - unlink a role from one connector, or the whole group (Manage Server)
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

    async def on_server_channel_create(self, event, /) -> None:
        await self._owner._handle_channel_create(event.channel)

    async def on_server_member_update(self, event, /) -> None:
        # TODO: unverified against a live server - event/attr shape assumed
        # from stoat.events.ServerMemberUpdateEvent (.before / .after Member).
        await self._owner._handle_member_update(event)


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
        category_linker: "CategoryLinker | None" = None,
        role_linker: "RoleLinker | None" = None,
        on_member_roles_changed: "OnMemberRolesChanged | None" = None,
    ) -> None:
        # linker/mirrorer/emote_linker/user_linker/category_linker/role_linker
        # are only needed to serve the corresponding `/link-*` / `/link ...`
        # commands; None is accepted (e.g. for tests) but those commands will
        # then report themselves unconfigured.
        SenderService.__init__(self, on_message, on_reaction, on_emoji_created, on_emoji_deleted)
        self._config = config
        self.server_id = config.server_id
        self.connector_id = config.id
        self._health = health
        self._linker = linker
        self._mirrorer = mirrorer
        self._emote_linker = emote_linker
        self._user_linker = user_linker
        self._category_linker = category_linker
        self._role_linker = role_linker
        self._on_member_roles_changed = on_member_roles_changed
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

    async def get_channel_category_name(self, channel_id: str) -> str | None:
        """Best-effort channel-id -> Category-title lookup, for `/mirror-
        channel` to carry a channel's Category across to the destination
        connector. `.category` can raise NoData on a cache miss, same
        best-effort pattern as get_channel_name/get_masquerade_identity
        elsewhere in this class.

        TODO: verify stoat.py's get_channel(partial=False) semantics - see
        get_channel_name's TODO above.
        """
        try:
            channel = self._client.get_channel(channel_id, partial=False)
            category = channel.category
        except Exception:
            return None
        return category.title if category is not None else None

    async def get_category_name(self, category_id: str) -> str | None:
        """Best-effort Category-id -> title lookup, used as this connector's
        `ConnectorInfo.resolve_category_name` for `/link-category`. Unlike
        get_channel_name/get_channel_category_name, there's no direct
        "get Category by id" call on stoat.Server, so this scans
        `server.categories` for a matching id instead."""
        try:
            server = self._client.get_server(self.server_id, partial=True)
            category = next((c for c in (server.categories or []) if str(c.id) == category_id), None)
        except Exception:
            return None
        return category.title if category is not None else None

    async def ensure_channel(
        self,
        name: str,
        category: str | None = None,
        is_thread_category: bool = False,
        category_parent_channel_id: str | None = None,
    ) -> str:
        """Idempotent get-or-create by name, for `/mirror-channel`'s
        `ConnectorInfo.ensure_channel` hook - same "match existing by name,
        else create" logic `_mirror_guild_structure` already uses in bulk,
        just for a single channel outside that flow. If `category` is given,
        the matched-or-created channel is placed into a same-named Category
        (creating it if needed) - best-effort, never raises, since the
        channel itself has already been secured by this point.
        `is_thread_category`, when True, binds that Category (via
        CategoryLinker.bind_thread_category) to `category_parent_channel_id`
        - this connector's own channel id for the thread's parent - as one
        Discord's thread/forum-post auto-mirroring created, so
        `/link-category` later refuses to link it and later threads for the
        same parent resolve the Category by id rather than title (surviving a
        rename). See DiscordSenderService._handle_thread_create."""
        # _ensure_channel_in_category needs a full Server (`.categories` /
        # `.channels`) - a BaseServer (what get_server(partial=True) yields
        # when the server isn't in cache) has neither, which silently
        # defeated category placement. Fetch the real thing if the cache
        # doesn't already hold it.
        server = self._client.get_server(self.server_id, partial=False)
        if not isinstance(server, stoat.Server):
            try:
                server = await self._client.fetch_server(self.server_id, populate_channels=True)
            except Exception:
                logger.exception(
                    "[stoat:%s] couldn't fetch full server %s; channel/category placement may be incomplete",
                    self.connector_id,
                    self.server_id,
                )
                server = self._client.get_server(self.server_id, partial=True)
        for channel in getattr(server, "channels", []):
            if channel.name == name:
                channel_id = channel.id
                break
        else:
            channel = await server.create_channel(name=name)
            channel_id = channel.id
        if category is not None:
            await self._ensure_channel_in_category(
                server, channel_id, category, is_thread_category, category_parent_channel_id
            )
        return channel_id

    async def _ensure_channel_in_category(
        self,
        server,
        channel_id: str,
        category: str,
        is_thread_category: bool = False,
        parent_channel_id: str | None = None,
    ) -> None:
        """Places `channel_id` into a Category on `server`, creating it if
        needed. When `parent_channel_id` is given and it's already bound to a
        thread Category (ThreadCategoryRepository), that Category is resolved
        by its stored id - so a Category rename on Stoat doesn't spawn a new
        one; a bound id that's since vanished from the server self-heals by
        forgetting the binding and falling back to the by-title path. Neither
        stoat.py's create_category nor edit_category takes a `position` - so a
        freshly-created Category is necessarily appended, landing at the
        bottom of the server's channel list with no extra work needed here."""
        bound_category_id: str | None = None
        if parent_channel_id is not None and self._category_linker is not None:
            try:
                bound = await self._category_linker.thread_category_id(self.connector_id, parent_channel_id)
            except Exception:
                logger.exception("[stoat:%s] couldn't look up bound thread category", self.connector_id)
                bound = None
            if bound is not None:
                if any(str(c.id) == bound for c in (getattr(server, "categories", None) or [])):
                    bound_category_id = bound
                else:
                    logger.info(
                        "[stoat:%s] bound thread category %s for parent %s is gone; rebinding",
                        self.connector_id,
                        bound,
                        parent_channel_id,
                    )
                    try:
                        await self._category_linker.forget_thread_category(self.connector_id, parent_channel_id)
                    except Exception:
                        logger.exception("[stoat:%s] couldn't forget stale thread category", self.connector_id)
        try:
            resolved = await self._place_in_category(server, channel_id, category, bound_category_id)
        except Exception:
            # A stale cached Server (categories/channels out of date - e.g. a
            # category created on an earlier run that isn't in this snapshot,
            # so create_category hits a duplicate) is the likeliest cause.
            # Re-fetch the real server once and retry against fresh state.
            logger.exception(
                "[stoat:%s] category placement for %r failed; re-fetching server and retrying",
                self.connector_id,
                category,
            )
            try:
                server = await self._client.fetch_server(self.server_id, populate_channels=True)
                resolved = await self._place_in_category(server, channel_id, category, bound_category_id)
            except Exception:
                logger.exception(
                    "[stoat:%s] category placement for %r failed on retry; channel %s left uncategorised",
                    self.connector_id,
                    category,
                    channel_id,
                )
                return
        logger.debug(
            "[stoat:%s] placed channel %s into category %r (%s)",
            self.connector_id,
            channel_id,
            category,
            resolved.id,
        )
        if is_thread_category and parent_channel_id is not None and self._category_linker is not None:
            try:
                await self._category_linker.bind_thread_category(
                    self.connector_id, parent_channel_id, str(resolved.id)
                )
            except Exception:
                logger.exception(
                    "[stoat:%s] failed to bind category %s to parent %s",
                    self.connector_id,
                    resolved.id,
                    parent_channel_id,
                )

    async def _place_in_category(self, server, channel_id: str, category: str, category_id: str | None = None):
        """Ensure `channel_id` is in a Category on `server`, creating one
        titled `category` if there's none. When `category_id` is given, the
        existing Category is matched by that id (title ignored - it may have
        been renamed); otherwise by title. Returns the resolved Category.
        Raises on API failure (the caller retries).

        Tries the dedicated create/edit-category endpoints first; every Stoat
        deployment tested (incl. "latest") 404s them - the installed stoat.py
        ships those routes ahead of the servers - so we fall back to PATCHing
        the whole category list onto the server."""
        categories = getattr(server, "categories", None) or []
        if category_id is not None:
            existing = next((c for c in categories if str(c.id) == category_id), None)
        else:
            existing = next((c for c in categories if c.title == category), None)
        try:
            if existing is None:
                return await server.create_category(category, channels=[channel_id])
            if channel_id not in existing.channels:
                await server.edit_category(existing, channels=[*existing.channels, channel_id])
            return existing
        except stoat.HTTPException as exc:
            # Both of the user's servers (incl. "latest") 404 the dedicated
            # categories endpoints - stoat.py ships routes ahead of the
            # deployed API - so this fallback is the normal path, not an error.
            logger.debug(
                "[stoat:%s] dedicated category endpoint unavailable (%s); using whole-server edit",
                self.connector_id,
                exc,
            )
            return await self._place_via_server_edit(server, channel_id, category, category_id)

    async def _place_via_server_edit(
        self, server, channel_id: str, category: str, category_id: str | None = None
    ):
        """Category placement for Stoat servers without the dedicated categories
        endpoint: PATCH the server with the full category list. The payload is
        built by hand (`{id, title, channels}` only) and sent straight through
        the HTTP layer - the installed stoat.py's `Category.to_dict()` trips
        over `default_permissions` on categories it parsed from an older
        server's payload, so `server.edit(categories=...)` can't be used.

        `category_id`, if given, matches the existing Category by id rather
        than title (see _place_in_category)."""
        raw_categories = [
            {"id": c.id, "title": c.title, "channels": list(getattr(c, "channels", None) or [])}
            for c in (getattr(server, "categories", None) or [])
        ]
        if category_id is not None:
            resolved = next((c for c in raw_categories if str(c["id"]) == category_id), None)
        else:
            resolved = next((c for c in raw_categories if c["title"] == category), None)
        if resolved is None:
            resolved = {"id": ulid_new(), "title": category, "channels": [channel_id]}
            raw_categories.append(resolved)
        elif channel_id not in resolved["channels"]:
            resolved["channels"].append(channel_id)
        await server.state.http.request(
            stoat_routes.SERVERS_SERVER_EDIT.compile(server_id=server.id),
            json={"categories": raw_categories},
        )
        return SimpleNamespace(id=resolved["id"], title=resolved["title"], channels=resolved["channels"])

    async def group_parent_channel_with_threads(self, thread_channel_id: str) -> None:
        """If `group_parent_channel_with_threads` is enabled and
        `thread_channel_id` (a channel a message was just relayed into) sits
        in a Category that Discord thread-mirroring created (see
        DiscordSenderService._handle_thread_create), pull the parent channel
        - the server channel whose name matches that Category's title - out
        of wherever it currently lives and place it first in the thread
        Category. So `#bot-config` and every thread spawned from it end up
        together under one "bot-config" Category.

        Re-checked on every relay (StoatReceiverService.receive calls this)
        so enabling the option mid-deployment takes effect without a restart.
        Best-effort: never raises, and only uses the already-cached full
        Server - no network fetch on the hot path - so it no-ops silently
        until the cache holds one with its Category list populated."""
        if not getattr(self._config, "group_parent_channel_with_threads", True):
            return
        if self._category_linker is None:
            return
        try:
            server = self._client.get_server(self.server_id, partial=False)
            categories = getattr(server, "categories", None)
            if categories is None or not hasattr(server, "state"):
                return
            thread_cat = next(
                (c for c in categories if thread_channel_id in (getattr(c, "channels", None) or [])), None
            )
            if thread_cat is None:
                return
            if not await self._category_linker.is_thread_category(self.connector_id, str(thread_cat.id)):
                return
            channels = getattr(server, "channels", [])
            bound_parent_id = await self._category_linker.thread_category_parent(
                self.connector_id, str(thread_cat.id)
            )
            if bound_parent_id is not None:
                parent = next((ch for ch in channels if str(ch.id) == bound_parent_id), None)
            else:
                # Legacy row with no parent binding - fall back to name match.
                parent = next((ch for ch in channels if ch.name == thread_cat.title), None)
            if parent is None:
                return
            if list(getattr(thread_cat, "channels", None) or [])[:1] == [parent.id]:
                return  # parent already grouped at the top - nothing to do
            await self._move_channel_to_category_top(server, parent.id, str(thread_cat.id))
            logger.info(
                "[stoat:%s] grouped parent channel %s (%r) atop thread category %s",
                self.connector_id,
                parent.id,
                thread_cat.title,
                thread_cat.id,
            )
        except Exception:
            logger.exception(
                "[stoat:%s] couldn't group parent channel with threads for channel %s",
                self.connector_id,
                thread_channel_id,
            )

    async def _move_channel_to_category_top(self, server, channel_id: str, category_id: str) -> None:
        """PATCH the server's whole Category list with `channel_id` removed
        from every Category and re-inserted at the front of `category_id`.
        Same hand-built payload / raw HTTP path as _place_via_server_edit
        (see its docstring for why `server.edit(categories=...)` can't be
        used)."""
        raw_categories = [
            {"id": c.id, "title": c.title, "channels": list(getattr(c, "channels", None) or [])}
            for c in (getattr(server, "categories", None) or [])
        ]
        target = next((c for c in raw_categories if c["id"] == category_id), None)
        if target is None:
            return
        for c in raw_categories:
            if channel_id in c["channels"]:
                c["channels"].remove(channel_id)
        target["channels"].insert(0, channel_id)
        await server.state.http.request(
            stoat_routes.SERVERS_SERVER_EDIT.compile(server_id=server.id),
            json={"categories": raw_categories},
        )

    async def _handle_channel_create(self, channel) -> None:
        """`_StoatClient.on_server_channel_create`'s target - auto-syncs a
        newly-created channel into every other connector's own linked
        Category, if the Category this channel appeared in on this server is
        itself linked via `/link-category`. Best-effort: never lets a sync
        failure or an unrelated channel (wrong server, no Category, Category
        linking not configured) propagate."""
        if self._category_linker is None or getattr(channel, "server_id", None) != self.server_id:
            return
        try:
            category = channel.category
        except Exception:
            category = None
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
            logger.exception("[stoat:%s] failed to auto-sync new channel %s", self.connector_id, channel.id)

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
        two = f"{parts[0].lower()} {parts[1].lower()}" if len(parts) > 1 else ""
        if two == "/link role":
            await self._handle_link_role(message, parts[2:])
            return
        if two == "/unlink role":
            await self._handle_unlink_role(message, parts[2:])
            return
        if two == "/linked roles":
            await self._handle_linked_roles(message, parts[2:])
            return
        if two == "/mirror role":
            await self._handle_mirror_role(message, parts[2:])
            return
        if cmd == "/status":
            await message.channel.send(self._health.render())
            return
        if cmd == "/bridge-help":
            await message.channel.send(_HELP_TEXT)
            return
        if cmd == "/linked-channels":
            await self._handle_linked_channels(message)
            return
        if cmd == "/linked-categories":
            await self._handle_linked_categories(message)
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
        if cmd == "/link-category":
            await self._handle_link_category(message, parts[1:])
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
        if cmd == "/unlink-category":
            await self._handle_unlink_category(message, parts[1:])
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

    async def _handle_linked_categories(self, message) -> None:
        if self._category_linker is None:
            await message.channel.send("Category linking isn't configured.")
            return
        category = _channel_category(message.channel)
        if category is None:
            await message.channel.send("This channel isn't in a Category.")
            return
        summary = await self._category_linker.list_linked_categories(
            local_connector=self.connector_id, local_category_id=str(category.id)
        )
        await message.channel.send(summary)

    async def _handle_linked_users(self, message, args: list[str], /) -> None:
        """`/linked-users [local_id]`: with no argument, lists every
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
        """`/mirror-channels <service>`: recreate `<service>`'s (a configured
        Discord connector's) category/channel layout on this Stoat server,
        linking each channel it creates or matches by name back to its
        Discord counterpart. Requires Manage Server so only admins can
        trigger a (potentially large) batch of channel creations.
        """
        if not self._is_admin(message):
            await message.channel.send("You need the Manage Server permission to do that.")
            return
        if not args:
            await message.channel.send("Usage: /mirror-channels <service>")
            return
        service = args[0]

        if self._mirrorer is None:
            await message.channel.send("Mirroring isn't configured.")
            return
        logger.info("[stoat:%s] %s ran /mirror-channels service=%s", self.connector_id, message.author.id, service)
        try:
            structure = self._mirrorer.get_structure(service)
        except LinkError as exc:
            logger.info("[stoat:%s] /mirror-channels rejected: %s", self.connector_id, exc)
            await message.channel.send(str(exc))
            return
        except Exception as exc:
            logger.exception("[stoat:%s] /mirror-channels couldn't read '%s' structure", self.connector_id, service)
            await message.channel.send(f"Couldn't read the '{service}' channel structure: {exc}")
            return

        summary = await _mirror_guild_structure(
            message.channel.server,
            structure,
            source=service,
            local_connector=self.connector_id,
            linker=self._linker,
        )
        await message.channel.send(summary)

    async def _handle_link_channel(self, message, args: list[str], /) -> None:
        if not self._is_admin(message):
            await message.channel.send("You need the Manage Server permission to do that.")
            return
        if len(args) < 2:
            await message.channel.send("Usage: /link-channel <service> <external_id> [<local_id>]")
            return
        service, external_id, *rest = args
        local_id = rest[0] if rest else None

        if self._linker is None:
            await message.channel.send("Linking isn't configured.")
            return
        logger.info(
            "[stoat:%s] %s ran /link-channel service=%s external_id=%s local_id=%s",
            self.connector_id,
            message.author.id,
            service,
            external_id,
            local_id,
        )
        try:
            summary = await self._linker.link_channel(
                local_connector=self.connector_id,
                local_channel_id=str(message.channel.id),
                local_channel_name=getattr(message.channel, "name", str(message.channel.id)),
                source=service,
                source_id=external_id,
                destination_id=local_id,
            )
        except LinkError as exc:
            logger.info("[stoat:%s] /link-channel rejected: %s", self.connector_id, exc)
            await message.channel.send(str(exc))
            return
        await message.channel.send(summary)

    async def _handle_link_category(self, message, args: list[str], /) -> None:
        """`/link-category <service> <external_id> [<local_id>]`: links
        the invoking channel's Category to `external_id`'s Category on
        `service` (or `local_id`'s Category on this connector, if
        given). Once linked, a new channel appearing in either Category
        auto-syncs onto the other."""
        if not self._is_admin(message):
            await message.channel.send("You need the Manage Server permission to do that.")
            return
        if len(args) < 2:
            await message.channel.send("Usage: /link-category <service> <external_id> [<local_id>]")
            return
        service, external_id, *rest = args
        local_id = rest[0] if rest else None

        if self._category_linker is None:
            await message.channel.send("Category linking isn't configured.")
            return
        category = _channel_category(message.channel)
        if category is None:
            await message.channel.send("This channel isn't in a Category.")
            return
        logger.info(
            "[stoat:%s] %s ran /link-category service=%s external_id=%s local_id=%s",
            self.connector_id,
            message.author.id,
            service,
            external_id,
            local_id,
        )
        try:
            summary = await self._category_linker.link_category(
                local_connector=self.connector_id,
                local_category_id=str(category.id),
                local_category_name=category.title,
                source=service,
                source_id=external_id,
                destination_id=local_id,
            )
        except LinkError as exc:
            logger.info("[stoat:%s] /link-category rejected: %s", self.connector_id, exc)
            await message.channel.send(str(exc))
            return
        await message.channel.send(summary)

    async def _handle_link_emote(self, message, args: list[str], /) -> None:
        if not self._is_admin(message):
            await message.channel.send("You need the Manage Server permission to do that.")
            return
        if len(args) < 3:
            await message.channel.send("Usage: /link-emote <service> <external_id> <local_id>")
            return
        service, external_id, local_id = args[:3]

        if self._emote_linker is None:
            await message.channel.send("Linking isn't configured.")
            return
        logger.info(
            "[stoat:%s] %s ran /link-emote service=%s external_id=%s local_id=%s",
            self.connector_id,
            message.author.id,
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
            logger.info("[stoat:%s] /link-emote rejected: %s", self.connector_id, exc)
            await message.channel.send(str(exc))
            return
        await message.channel.send(summary)

    async def _handle_link_user(self, message, args: list[str], /) -> None:
        if not self._is_admin(message):
            await message.channel.send("You need the Manage Server permission to do that.")
            return
        if len(args) < 3:
            await message.channel.send("Usage: /link-user <service> <external_id> <local_id>")
            return
        service, external_id, local_id = args[0], args[1], args[2]

        if self._user_linker is None:
            await message.channel.send("User linking isn't configured.")
            return
        logger.info(
            "[stoat:%s] %s ran /link-user service=%s external_id=%s local_id=%s",
            self.connector_id,
            message.author.id,
            service,
            external_id,
            local_id,
        )
        try:
            summary = await self._user_linker.link_user(
                local_connector=self.connector_id,
                local_user_id=local_id,
                source=service,
                source_user_id=external_id,
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
            await message.channel.send("Usage: /mirror-channel <service|all> [local_id]")
            return
        service = args[0]
        if len(args) > 1:
            channel_id = channel_name = args[1]  # explicit id - no way to resolve its real display name
        else:
            channel_id = str(message.channel.id)
            channel_name = getattr(message.channel, "name", channel_id)

        if self._linker is None:
            await message.channel.send("Linking isn't configured.")
            return
        channel_category = await self.get_channel_category_name(channel_id)
        logger.info(
            "[stoat:%s] %s ran /mirror-channel service=%s local_id=%s",
            self.connector_id,
            message.author.id,
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
            logger.info("[stoat:%s] /mirror-channel rejected: %s", self.connector_id, exc)
            await message.channel.send(str(exc))
            return
        await message.channel.send(summary)

    async def _handle_unlink_channel(self, message, args: list[str], /) -> None:
        """`/unlink-channel [service|all] [local_id]`: service
        defaults to "all" (dissolving the whole bridge group);
        local_id defaults to the invoking channel."""
        if not self._is_admin(message):
            await message.channel.send("You need the Manage Server permission to do that.")
            return
        service = args[0] if args else None
        channel_id = args[1] if len(args) > 1 else str(message.channel.id)

        if self._linker is None:
            await message.channel.send("Linking isn't configured.")
            return
        logger.info(
            "[stoat:%s] %s ran /unlink-channel service=%s local_id=%s",
            self.connector_id,
            message.author.id,
            service,
            channel_id,
        )
        try:
            summary = await self._linker.unlink_channel(
                local_connector=self.connector_id, local_channel_id=channel_id, destination=service
            )
        except LinkError as exc:
            logger.info("[stoat:%s] /unlink-channel rejected: %s", self.connector_id, exc)
            await message.channel.send(str(exc))
            return
        await message.channel.send(summary)

    async def _handle_unlink_category(self, message, args: list[str], /) -> None:
        """`/unlink-category [service|all]`: service defaults to
        "all" (dissolving the whole bridge group); the Category is always
        the invoking channel's own Category."""
        if not self._is_admin(message):
            await message.channel.send("You need the Manage Server permission to do that.")
            return
        service = args[0] if args else None

        if self._category_linker is None:
            await message.channel.send("Category linking isn't configured.")
            return
        category = _channel_category(message.channel)
        if category is None:
            await message.channel.send("This channel isn't in a Category.")
            return
        logger.info(
            "[stoat:%s] %s ran /unlink-category service=%s category_id=%s",
            self.connector_id,
            message.author.id,
            service,
            category.id,
        )
        try:
            summary = await self._category_linker.unlink_category(
                local_connector=self.connector_id, local_category_id=str(category.id), destination=service
            )
        except LinkError as exc:
            logger.info("[stoat:%s] /unlink-category rejected: %s", self.connector_id, exc)
            await message.channel.send(str(exc))
            return
        await message.channel.send(summary)

    async def _handle_unlink_user(self, message, args: list[str], /) -> None:
        """`/unlink-user [service|all] [local_id]`: service defaults
        to "all" (dissolving the whole link group); local_id defaults to the
        invoking user themselves."""
        if not self._is_admin(message):
            await message.channel.send("You need the Manage Server permission to do that.")
            return
        service = args[0] if args else None
        local_id = args[1] if len(args) > 1 else str(message.author.id)

        if self._user_linker is None:
            await message.channel.send("User linking isn't configured.")
            return
        logger.info(
            "[stoat:%s] %s ran /unlink-user service=%s local_id=%s",
            self.connector_id,
            message.author.id,
            service,
            local_id,
        )
        try:
            summary = await self._user_linker.unlink_user(
                local_connector=self.connector_id, local_user_id=local_id, destination=service
            )
        except LinkError as exc:
            logger.info("[stoat:%s] /unlink-user rejected: %s", self.connector_id, exc)
            await message.channel.send(str(exc))
            return
        await message.channel.send(summary)

    async def _handle_link_role(self, message, args: list[str], /) -> None:
        """`/link role <local_id|name> <service> <external_id|name>`."""
        if not self._is_admin(message):
            await message.channel.send("You need the Manage Server permission to do that.")
            return
        if self._role_linker is None:
            await message.channel.send("Role linking isn't configured.")
            return
        if len(args) < 3:
            await message.channel.send("Usage: /link role <local_id|name> <service> <external_id|name>")
            return
        local_id, service, external_id = args[0], args[1], args[2]
        logger.info(
            "[stoat:%s] %s ran /link role local=%s service=%s external=%s",
            self.connector_id,
            message.author.id,
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
            logger.info("[stoat:%s] /link role rejected: %s", self.connector_id, exc)
            await message.channel.send(str(exc))
            return
        await message.channel.send(summary)

    async def _handle_unlink_role(self, message, args: list[str], /) -> None:
        """`/unlink role <local_id|name> [<service>|all]`."""
        if not self._is_admin(message):
            await message.channel.send("You need the Manage Server permission to do that.")
            return
        if self._role_linker is None:
            await message.channel.send("Role linking isn't configured.")
            return
        if not args:
            await message.channel.send("Usage: /unlink role <local_id|name> [<service>|all]")
            return
        local_id = args[0]
        service = args[1] if len(args) > 1 else None
        try:
            summary = await self._role_linker.unlink_role(
                local_connector=self.connector_id, local_role=local_id, destination=service
            )
        except LinkError as exc:
            logger.info("[stoat:%s] /unlink role rejected: %s", self.connector_id, exc)
            await message.channel.send(str(exc))
            return
        await message.channel.send(summary)

    async def _handle_linked_roles(self, message, args: list[str], /) -> None:
        """`/linked roles [<local_id|name>] [<service>|all]` - read-only."""
        if self._role_linker is None:
            await message.channel.send("Role linking isn't configured.")
            return
        local_id = args[0] if args else None
        service = args[1] if len(args) > 1 else None
        summary = await self._role_linker.list_linked_roles(
            local_connector=self.connector_id, local_role=local_id, service=service
        )
        await message.channel.send(summary)

    async def _handle_mirror_role(self, message, args: list[str], /) -> None:
        """`/mirror role <local_id|name> [<service>|all]`."""
        if not self._is_admin(message):
            await message.channel.send("You need the Manage Server permission to do that.")
            return
        if self._role_linker is None:
            await message.channel.send("Role linking isn't configured.")
            return
        if not args:
            await message.channel.send("Usage: /mirror role <local_id|name> [<service>|all]")
            return
        local_id = args[0]
        service = args[1] if len(args) > 1 else None
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
            logger.info("[stoat:%s] /mirror role rejected: %s", self.connector_id, exc)
            await message.channel.send(str(exc))
            return
        await message.channel.send(summary)

    async def get_role_name(self, role_id: str) -> str | None:
        """Best-effort role-id -> name lookup, this connector's
        `ConnectorInfo.resolve_role_name`."""
        try:
            role = self._role_by_id(role_id)
        except Exception:
            return None
        return getattr(role, "name", None) if role is not None else None

    async def resolve_role_id_by_name(self, token: str) -> str | None:
        """Resolve a bare role name to its id (case-insensitive, first match);
        a token that's already a role id is returned as-is, an unknown token
        yields None."""
        try:
            roles = self._all_roles()
        except Exception:
            return None
        for role in roles:
            if str(getattr(role, "id", "")) == token:
                return token
        lowered = token.casefold()
        for role in roles:
            if str(getattr(role, "name", "")).casefold() == lowered:
                return str(role.id)
        return None

    async def ensure_role(self, name: str) -> str:
        """Get-or-create a role named `name`, returning its id - this
        connector's `ConnectorInfo.ensure_role` for `/mirror role`."""
        server = self._client.get_server(self.server_id, partial=False)
        if not isinstance(server, stoat.Server):
            server = await self._client.fetch_server(self.server_id)
        lowered = name.casefold()
        for role in self._roles_of(server):
            if str(getattr(role, "name", "")).casefold() == lowered:
                return str(role.id)
        role = await server.create_role(name=name)
        return str(role.id)

    @staticmethod
    def _roles_of(server):
        roles = getattr(server, "roles", None) or []
        return list(roles.values()) if isinstance(roles, dict) else list(roles)

    def _all_roles(self):
        server = self._client.get_server(self.server_id, partial=True)
        return self._roles_of(server)

    def _role_by_id(self, role_id: str):
        return next((r for r in self._all_roles() if str(getattr(r, "id", "")) == role_id), None)

    async def _handle_member_update(self, event) -> None:
        """A server member changed - diff their role id set for role
        auto-grant. TODO: attr shape (event.before / event.after, both
        Member with .role_ids) assumed from stoat.events - unverified."""
        if self._on_member_roles_changed is None:
            return
        before = getattr(event, "before", None)
        after = getattr(event, "after", None)
        if after is None:
            return
        if getattr(after, "server_id", self.server_id) != self.server_id:
            return
        before_ids = {str(r) for r in (getattr(before, "role_ids", []) or [])} if before is not None else set()
        after_ids = {str(r) for r in (getattr(after, "role_ids", []) or [])}
        added = after_ids - before_ids
        removed = before_ids - after_ids
        if not added and not removed:
            return
        user_id = str(getattr(after, "id", "") or getattr(event, "member", SimpleNamespace(id="")).id)
        await self._on_member_roles_changed(self.connector_id, user_id, added, removed)

    async def grant_role(self, user_id: str, role_id: str) -> None:
        """Idempotent (no-op if the member already has the role) so the
        grant echo doesn't loop. Best-effort. Note stoat.py's Member.edit
        REPLACES the whole role list - this is a read-modify-write."""
        await self._edit_member_roles(user_id, role_id, add=True)

    async def revoke_role(self, user_id: str, role_id: str) -> None:
        await self._edit_member_roles(user_id, role_id, add=False)

    async def _edit_member_roles(self, user_id: str, role_id: str, *, add: bool) -> None:
        try:
            member = await self._client.get_server(self.server_id, partial=True).fetch_member(user_id)
        except Exception:
            logger.warning("[stoat:%s] role sync: couldn't fetch member %s", self.connector_id, user_id)
            return
        current = [str(r) for r in (getattr(member, "role_ids", []) or [])]
        has = role_id in current
        if has == add:
            return
        if add:
            current.append(role_id)
        else:
            current = [r for r in current if r != role_id]
        try:
            await member.edit(roles=current)
        except Exception:
            logger.exception(
                "[stoat:%s] role sync: %s role %s for %s failed",
                self.connector_id,
                "add" if add else "remove",
                role_id,
                user_id,
            )

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

    def __init__(
        self,
        sender: StoatSenderService,
        user_mappings: UserMappingRepository | None = None,
        channel_mappings: ChannelMappingRepository | None = None,
        role_mappings: RoleMappingRepository | None = None,
    ) -> None:
        self.connector_id = sender.connector_id
        self._sender = sender
        self._user_mappings = user_mappings
        self._channel_mappings = channel_mappings
        self._role_mappings = role_mappings

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
        if self._channel_mappings is not None:
            content = await rewrite_channel_mentions(
                content,
                origin_connector_id=message.origin_connector_id,
                target_connector_id=self.connector_id,
                target_kind="stoat",
                channel_mappings=self._channel_mappings,
            )
        if self._role_mappings is not None:
            content = await rewrite_role_mentions(
                content,
                origin_connector_id=message.origin_connector_id,
                target_connector_id=self.connector_id,
                target_kind="stoat",
                role_mappings=self._role_mappings,
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
        # Best-effort, never fatal to the relay: keep a thread Category's
        # parent channel grouped at its top (see the sender method's docstring).
        await self._sender.group_parent_channel_with_threads(target_channel_id)
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


def _channel_category(channel):
    """Best-effort `channel.category` read - the property can raise NoData
    on a cache miss (same caveat as StoatSenderService.get_channel_category_
    name), so command handlers that need "the Category this channel is in"
    go through this rather than touching `.category` directly."""
    try:
        return channel.category
    except Exception:
        return None


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
