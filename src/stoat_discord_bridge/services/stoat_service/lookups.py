"""Platform-resource lookups for the Stoat connector.

The `ConnectorInfo`-hook half of `StoatSenderService`: resolving ids to
names and vice versa, get-or-create for channels / roles / categories /
emoji, and the Category-placement plumbing (`/mirror channel`, Discord
thread mirroring). All keyed off `self.server_id` / `self._client` /
`self._category_linker`, which the composed service provides.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace

import stoat
from stoat import routes as stoat_routes
from stoat.core import ulid_new

from stoat_discord_bridge.models import ChannelMetadata, CustomEmoji
from stoat_discord_bridge.services.caching import AsyncTTLCache, RefreshThrottle
from stoat_discord_bridge.services.stoat_service.formatting import _avatar_url, _display_name, _download

logger = logging.getLogger(__name__)

# Stoat channel descriptions are capped at 1024 chars (stoat.py
# Server.create_channel); a longer source description is clipped rather than
# rejected.
_DESCRIPTION_LIMIT = 1024

# How long a freshly-fetched Category list is reused before another
# `fetch_server` - see `_fresh_categories`. Short: a human creating a Category
# on Stoat then linking it via `/link category` (issue #66) shouldn't have to
# wait, but the Discord slash-command autocomplete calls the `list_*` hooks
# repeatedly while someone types, and once one load has landed the rest of
# that burst is served from here rather than re-fetching.
_CATEGORY_CACHE_TTL = 15.0

# How long after a successful `refresh()` the next one is skipped (issue
# #81). A single `/mirror <noun> all` or `/mirror category` re-enters the
# per-destination/per-child mirror many times in a row; the throttle
# collapses that whole burst to one server re-fetch. Two separate `/mirror`
# commands run within this window also share the one refresh - an accepted
# trade (see RefreshThrottle).
_REFRESH_MIN_INTERVAL = 10.0


def _undefined_cache_context():
    """stoat.py's cache writers (`store_server` / `store_channel` / ...) all
    take a `BaseCacheContext`. Outside an event handler there's no natural
    one, so hand them the library's "undefined" context - the same thing its
    own `ReadyEvent` uses. Returns None if neither the module singleton nor
    the class is importable (a stoat.py that's moved them), letting `refresh`
    skip the cache write entirely."""
    try:
        from stoat.cache import _UNDEFINED

        return _UNDEFINED
    except Exception:
        pass
    try:
        from stoat.cache import CacheContextType, UndefinedCacheContext

        return UndefinedCacheContext(type=CacheContextType.undefined)
    except Exception:
        return None


def _create_channel_metadata_kwargs(metadata: ChannelMetadata | None) -> dict:
    """The `description=` / `nsfw=` kwargs for `Server.create_channel` from a
    `ChannelMetadata` (issue #32) - empty when there's nothing to carry, so a
    metadata-less mirror creates a channel exactly as before. Icon isn't set
    here (it needs a follow-up `channel.edit` - see `_apply_channel_icon`)."""
    if metadata is None:
        return {}
    kwargs: dict = {}
    if metadata.description:
        kwargs["description"] = metadata.description[:_DESCRIPTION_LIMIT]
    if metadata.nsfw:
        kwargs["nsfw"] = True
    return kwargs


class StoatLookupsMixin:
    """Resource-lookup half of `StoatSenderService`."""

    def get_channel(self, channel_id: str, *, partial: bool = False):
        """Cache-only channel lookup (no network). Verified against stoat.py
        1.2.1 (`Client.get_channel`, `MapCache.get_channel`), not yet against
        a live server:

        - `partial=False` returns the fully-populated cached channel object
          (a `ServerChannel` - `.name` / `.category` / `.role_permissions` /
          `.category_id` all present) or `None` on a cache miss. It never
          raises for a missing channel and never does I/O.
        - `partial=True` returns that same cached channel, or a bare
          `PartialMessageable` stub (id + `Messageable` send/typing/
          fetch_message only - no `.name` etc.) on a miss, never `None`.

        So the `.name`/`.category`/`.role_permissions` readers below want
        `partial=False` and a `None` guard; the send/typing/fetch_message
        paths (receiver, reactions, pins) want `partial=True`.
        """
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

    async def can_view_channel(self, channel_id: str) -> bool | None:
        """`ConnectorInfo.can_view_channel`: True if the bridge bot can see
        `channel_id` on this server, False if the channel resolves but the
        bot's roles leave it without `view_channel` there, None if it can't
        tell (uncached channel, no self id yet, unresolvable bot member, or
        an error). `/mirror channel` refuses on an explicit False so a
        private channel the bot can't see is never mirrored (issue #33)."""
        if getattr(self, "_self_id", None) is None:
            return None
        channel = self._client.get_channel(channel_id, partial=False)
        if channel is None or not hasattr(channel, "permissions_for"):
            return None
        try:
            member = await self._client.get_server(self.server_id, partial=True).fetch_member(self._self_id)
        except Exception:
            logger.debug("[stoat:%s] couldn't fetch own member for channel-visibility check", self.connector_id)
            return None
        if member is None:
            return None
        try:
            return bool(channel.permissions_for(member).view_channel)
        except Exception:
            return None

    async def get_channel_name(self, channel_id: str) -> str | None:
        """Best-effort channel-id -> name lookup, used as this connector's
        `ConnectorInfo.resolve_channel_name` for `/link channel`.

        `get_channel(partial=False)` returns a fully-populated cached channel
        (`.name` present) or `None` on a cache miss - see `get_channel`'s
        docstring. The `getattr(..., None)` covers the miss; the `try` guards
        an unexpected raise only.
        """
        try:
            channel = self._client.get_channel(channel_id, partial=False)
        except Exception:
            return None
        return getattr(channel, "name", None)

    async def resolve_channel_id_by_name(self, token: str) -> str | None:
        """Resolve a bare channel name to its id so the `/link channel` etc.
        commands accept either - this connector's
        `ConnectorInfo.resolve_channel_id_by_name`. A token that's already a
        real channel id is returned as-is; an unrecognized token yields None
        (ChannelLinker then treats it as a literal id). Case-insensitive;
        first match wins."""
        try:
            server = self._client.get_server(self.server_id, partial=True)
            channels = list(getattr(server, "channels", []) or [])
        except Exception:
            return None
        if any(str(c.id) == token for c in channels):
            return token
        lowered = token.casefold()
        for channel in channels:
            if getattr(channel, "name", "").casefold() == lowered:
                return str(channel.id)
        return None

    async def get_channel_category(self, channel_id: str) -> tuple[str, str] | None:
        """Best-effort channel-id -> (Category-id, Category-title), or None if
        uncategorized / unresolvable. This connector's
        `ConnectorInfo.resolve_channel_category`, used by `/mirror channel
        from` to land the new local channel in the linked local Category.
        `get_channel(partial=False)` returns the cached channel or `None` on a
        miss (see `get_channel`'s docstring); `None.category` and a genuine
        cache-miss `NoData` from `.category` both land in the `except` and
        yield `None`, same best-effort pattern as get_channel_name elsewhere
        in this class."""
        try:
            channel = self._client.get_channel(channel_id, partial=False)
            category = channel.category
        except Exception:
            return None
        if category is None:
            return None
        return str(category.id), category.title

    async def get_channel_category_name(self, channel_id: str) -> str | None:
        """Best-effort channel-id -> Category-title lookup, for `/mirror
        channel` to carry a channel's Category across to the destination
        connector."""
        resolved = await self.get_channel_category(channel_id)
        return resolved[1] if resolved is not None else None

    async def get_category_name(self, category_id: str) -> str | None:
        """Best-effort Category-id -> title lookup, used as this connector's
        `ConnectorInfo.resolve_category_name` for `/link-category`. Unlike
        get_channel_name/get_channel_category_name, there's no direct
        "get Category by id" call on stoat.Server, so this scans the Category
        list for a matching id instead - the freshly-fetched one, since the
        cache doesn't track Categories added since startup (see
        `_fresh_categories`, issue #66)."""
        try:
            categories = await self._fresh_categories()
            category = next((c for c in categories if str(c.id) == category_id), None)
            return category.title if category is not None else None
        except Exception:
            return None

    async def ensure_channel(
        self,
        name: str,
        category: str | None = None,
        is_thread_category: bool = False,
        category_parent_channel_id: str | None = None,
        *,
        metadata: ChannelMetadata | None = None,
    ) -> str:
        """Idempotent get-or-create by name, for `/mirror channel`'s
        `ConnectorInfo.ensure_channel` hook - matches an existing channel by
        name, else creates one. If `category` is given,
        the matched-or-created channel is placed into a same-named Category
        (creating it if needed) - best-effort, never raises, since the
        channel itself has already been secured by this point.
        `is_thread_category`, when True, binds that Category (via
        CategoryLinker.bind_thread_category) to `category_parent_channel_id`
        - this connector's own channel id for the thread's parent - as one
        Discord's thread/forum-post auto-mirroring created, so
        `/link-category` later refuses to link it and later threads for the
        same parent resolve the Category by id rather than title (surviving a
        rename). See DiscordSenderService._handle_thread_create.

        `metadata`, when given, is the source channel's description / NSFW
        flag / icon - applied *only when this call creates the channel*
        (issue #32); a mirror that matched an existing channel leaves its
        metadata untouched. The icon is a best-effort download-and-set that
        never blocks the create from succeeding."""
        # Fetch the server fresh rather than trust the cache. Beyond needing a
        # full Server (`.categories` / `.channels`) instead of a BaseServer,
        # the channel-name dedupe below and `_ensure_channel_in_category`'s
        # bound-thread-Category check both read the whole channel/category
        # list - and the cache is populated once at gateway-connect, blind to
        # the raw-HTTP category edits this module makes, so a stale snapshot
        # spawns duplicate channels and duplicate thread Categories (issue
        # #27). ensure_channel isn't a hot path (admin commands + Discord
        # thread mirroring), so always re-fetch; fall back to the cache only
        # if that fails.
        try:
            server = await self._client.fetch_server(self.server_id, populate_channels=True)
        except Exception:
            logger.exception(
                "[stoat:%s] couldn't fetch full server %s; channel/category placement may be incomplete",
                self.connector_id,
                self.server_id,
            )
            server = self._client.get_server(self.server_id, partial=False)
            if not isinstance(server, stoat.Server):
                server = self._client.get_server(self.server_id, partial=True)
        for channel in getattr(server, "channels", []):
            if channel.name == name:
                channel_id = channel.id
                break
        else:
            channel = await server.create_channel(name=name, **_create_channel_metadata_kwargs(metadata))
            channel_id = channel.id
            if metadata is not None and metadata.icon_url:
                await self._apply_channel_icon(channel, metadata.icon_url)
        if category is not None:
            await self._ensure_channel_in_category(
                server, channel_id, category, is_thread_category, category_parent_channel_id
            )
        return channel_id

    async def _apply_channel_icon(self, channel, icon_url: str) -> None:
        """Best-effort: download `icon_url` and set it as `channel`'s icon.
        Only reached from `ensure_channel`'s create path. Never raises - a
        mirrored channel with no icon is still a working channel."""
        try:
            image_bytes = await _download(icon_url)
            await channel.edit(icon=stoat.Upload.icon(image_bytes, filename="icon.png"))
        except Exception:
            logger.warning(
                "[stoat:%s] couldn't set mirrored channel %s icon from %s",
                self.connector_id,
                getattr(channel, "id", "?"),
                icon_url,
                exc_info=True,
            )

    async def describe_channel(self, channel_id: str) -> ChannelMetadata | None:
        """Best-effort read of a channel's description / NSFW flag / icon URL
        as a `ChannelMetadata`, this connector's `ConnectorInfo.describe_channel`
        - `/mirror channel` reads it off the source channel so the mirrored
        copy isn't left blank (issue #32). Cache-only (same `partial=False`
        pattern as `get_channel_name`); None if the channel isn't resolvable."""
        try:
            channel = self._client.get_channel(channel_id, partial=False)
        except Exception:
            return None
        if channel is None:
            return None
        icon = getattr(channel, "icon", None)
        icon_url = None
        if icon is not None:
            try:
                icon_url = icon.url()
            except Exception:
                icon_url = None
        return ChannelMetadata(
            description=getattr(channel, "description", None),
            nsfw=bool(getattr(channel, "nsfw", False)),
            icon_url=icon_url,
        )

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
        bottom of the server's channel list with no extra work needed here.

        For a thread Category (`is_thread_category`), the parent channel is
        also pulled to the top of it here (see `group_parent_channel_with_threads`
        for the same move on the relay path) - gated by the per-connector
        `group_parent_channel_with_threads` option (issue #94)."""
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
            # `server` is already a fresh fetch from ensure_channel, but a
            # concurrent edit (or a fetch that fell back to the cache) can
            # still leave it out of date - e.g. a category that isn't in this
            # snapshot, so create_category hits a duplicate. Re-fetch once
            # more and retry against the newest state.
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
                    "[stoat:%s] category placement for %r failed on retry; channel %s left uncategorized",
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
            # Pull the thread's parent channel up into the thread Category right
            # now, rather than leaving it to `group_parent_channel_with_threads`
            # on the next relayed message: that reads the cache-only Category
            # list, which never carries a Category this module just created over
            # raw HTTP, so it no-ops until a reconnect or a `/mirror` `refresh()`
            # repopulates the cache - by which point `/mirror channel` on a
            # Discord thread has long since finished without grouping the parent
            # (issue #94). Gated by the same per-connector option and skipped
            # when the parent is missing / already on top.
            group_parent = getattr(getattr(self, "_config", None), "group_parent_channel_with_threads", True)
            parent_present = any(
                str(getattr(ch, "id", ch)) == parent_channel_id for ch in (getattr(server, "channels", None) or [])
            )
            already_on_top = list(getattr(resolved, "channels", None) or [])[:1] == [parent_channel_id]
            if group_parent and parent_present and not already_on_top:
                try:
                    await self._move_channel_to_category_top(server, parent_channel_id, str(resolved.id))
                    logger.info(
                        "[stoat:%s] grouped parent channel %s atop thread category %s",
                        self.connector_id,
                        parent_channel_id,
                        resolved.id,
                    )
                except Exception:
                    logger.exception(
                        "[stoat:%s] couldn't group parent channel %s atop thread category %s",
                        self.connector_id,
                        parent_channel_id,
                        resolved.id,
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
                created = await server.create_category(category, channels=[channel_id])
                self._invalidate_category_cache()
                return created
            if channel_id not in existing.channels:
                await server.edit_category(existing, channels=[*existing.channels, channel_id])
                self._invalidate_category_cache()
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

    async def _full_category_list(self, fallback=None):
        """`(server, raw_categories)` for a whole-server category PATCH.

        The category list is rebuilt from a *freshly fetched* Server, never the
        cached one: the cache's `.categories` is populated once at gateway
        connect and doesn't track the raw-HTTP category edits this module
        itself makes (nor any a human makes on Stoat directly), so PATCHing
        that stale snapshot straight back reverts the server's whole category
        layout to how it looked at startup and can delete-and-recreate a
        linked Category that was added or renamed since (issue #27). Falls back
        to `fallback` (or the cache) only if the re-fetch fails or yields
        something without a category list.

        Each entry keeps every field Stoat sent - `default_permissions` /
        `role_permissions` included, via `Category.to_dict()` - not just
        `id`/`title`/`channels`, so a category's permission overrides aren't
        wiped by an unrelated `/mirror channel` / `/mirror category`.
        `to_dict()` raising (older stoat.py chokes on `default_permissions`
        parsed from an older server's payload) drops that one entry back to
        the minimal shape."""
        server = None
        try:
            fetched = await self._client.fetch_server(self.server_id, populate_channels=True)
            if getattr(fetched, "categories", None) is not None:
                server = fetched
        except Exception:
            logger.exception(
                "[stoat:%s] couldn't re-fetch server %s before a category edit; using cached state",
                self.connector_id,
                self.server_id,
            )
        if server is None:
            server = fallback if fallback is not None else self._client.get_server(self.server_id, partial=False)
        raw_categories = []
        for c in getattr(server, "categories", None) or []:
            try:
                raw = dict(c.to_dict())
            except Exception:
                raw = {}
            raw.setdefault("id", getattr(c, "id", None))
            raw.setdefault("title", getattr(c, "title", None))
            raw["channels"] = list(raw.get("channels") or getattr(c, "channels", None) or [])
            raw_categories.append(raw)
        return server, raw_categories

    async def _place_via_server_edit(
        self, server, channel_id: str, category: str, category_id: str | None = None
    ):
        """Category placement for Stoat servers without the dedicated categories
        endpoint: PATCH the server with the full category list, built by hand
        and sent straight through the HTTP layer (the installed stoat.py's
        `server.edit(categories=...)` can't round-trip categories it parsed
        from an older server's payload). The list comes from
        `_full_category_list` - a fresh fetch, not the cached server - so the
        PATCH can't revert the layout (issue #27).

        `category_id`, if given, matches the existing Category by id rather
        than title (see _place_in_category)."""
        server, raw_categories = await self._full_category_list(server)
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
        self._invalidate_category_cache()
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
        Best-effort: never raises. The decision of whether a regroup is needed
        is made off the already-cached full Server (no I/O on the common
        no-op path - it stays quiet until the cache holds one with its
        Category list populated); only when a move is actually due does
        `_move_channel_to_category_top` re-fetch to PATCH from fresh state."""
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
        Same fresh-fetch / full-fidelity / raw HTTP path as
        `_place_via_server_edit` (see `_full_category_list` for why the cached
        server can't be PATCHed straight back - issue #27)."""
        server, raw_categories = await self._full_category_list(server)
        target = next((c for c in raw_categories if str(c["id"]) == category_id), None)
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
        self._invalidate_category_cache()

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

    async def _all_emojis(self) -> list:
        """Every custom emoji on this server - `server.emojis` if the cache
        has it, else a REST fetch. `Server.emojis` is a Mapping[id, emoji],
        so iterate its `.values()`, not the mapping itself (which yields ids)."""
        try:
            server = self._client.get_server(self.server_id, partial=True)
            emojis = getattr(server, "emojis", None) or {}
        except Exception:
            emojis = {}
        values = list(getattr(emojis, "values", lambda: emojis)())
        if values:
            return values
        try:
            server = await self._full_server()
            return list(await server.fetch_emojis())
        except Exception:
            logger.debug("[stoat:%s] fetch_emojis failed", self.connector_id, exc_info=True)
            return []

    async def get_emoji_name(self, emoji_id: str) -> str | None:
        """Best-effort emoji-id -> name lookup, this connector's
        `ConnectorInfo.resolve_emoji_name`."""
        try:
            server = self._client.get_server(self.server_id, partial=True)
            emoji = server.get_emoji(emoji_id)
        except Exception:
            emoji = None
        if emoji is None:
            emoji = next((e for e in await self._all_emojis() if str(e.id) == emoji_id), None)
        return getattr(emoji, "name", None) if emoji is not None else None

    async def resolve_emoji_id_by_name(self, token: str) -> str | None:
        """Resolve a bare custom-emoji name to its id (case-insensitive, first
        match); a token that's already an emoji id is returned as-is, an
        unknown token yields None - this connector's
        `ConnectorInfo.resolve_emoji_id_by_name`."""
        emojis = await self._all_emojis()
        if any(str(getattr(e, "id", "")) == token for e in emojis):
            return token
        lowered = token.casefold()
        for e in emojis:
            if str(getattr(e, "name", "")).casefold() == lowered:
                return str(e.id)
        return None

    async def resolve_emoji(self, emoji_id: str) -> "CustomEmoji | None":
        """emoji-id -> full CustomEmoji, this connector's
        `ConnectorInfo.resolve_emoji` (the source side of `/mirror emote`)."""
        try:
            server = self._client.get_server(self.server_id, partial=True)
            emoji = server.get_emoji(emoji_id)
        except Exception:
            emoji = None
        if emoji is None:
            emoji = next((e for e in await self._all_emojis() if str(e.id) == emoji_id), None)
        if emoji is None:
            return None
        return CustomEmoji(
            native_id=str(emoji.id),
            name=emoji.name,
            image_url=emoji.image.url(),
            animated=getattr(emoji, "animated", False),
        )

    async def _full_server(self):
        server = self._client.get_server(self.server_id, partial=False)
        if not isinstance(server, stoat.Server):
            server = await self._client.fetch_server(self.server_id)
        return server

    def _category_list_cache(self) -> "AsyncTTLCache[list]":
        """The short-TTL cache backing `_fresh_categories`, created lazily so
        the mixin works when a test builds the service with `object.__new__`
        and doesn't run `__init__`."""
        cache = getattr(self, "_category_cache", None)
        if cache is None:
            cache = AsyncTTLCache(_CATEGORY_CACHE_TTL)
            self._category_cache = cache
        return cache

    async def _fresh_categories(self) -> list:
        """This server's Category list from a *freshly fetched* Server, reused
        for `_CATEGORY_CACHE_TTL`s.

        stoat.py 1.2.1 populates the cached server's `.categories` once at
        gateway connect and never updates it from gateway events, so the
        cache-only readers below (`get_category_name`,
        `resolve_category_id_by_name`, `list_categories`,
        `channels_in_category`) miss any Category created or renamed since
        startup - `/link category` then can't map a typed name to a real id
        and stores the raw token instead, and autocomplete omits it (issue
        #66). Re-fetching fixes that; the TTL keeps a typing burst's follow-up
        autocomplete calls off the API once one load has landed (it doesn't
        de-dupe calls made concurrently before that). Falls back to the cached
        server's list if the fetch fails.
        """

        async def _load(_key: str) -> list:
            try:
                server = await self._client.fetch_server(self.server_id, populate_channels=True)
                if getattr(server, "categories", None) is not None:
                    return list(server.categories)
            except Exception:
                logger.debug(
                    "[stoat:%s] couldn't fetch a fresh Category list; using cached state",
                    self.connector_id,
                    exc_info=True,
                )
            try:
                server = self._client.get_server(self.server_id, partial=True)
                return list(getattr(server, "categories", None) or [])
            except Exception:
                return []

        return await self._category_list_cache().get(self.server_id, _load)

    def _invalidate_category_cache(self) -> None:
        """Drop the `_fresh_categories` cache after this module writes the
        Category layout, so a follow-up read in the same admin-command flow
        sees the change immediately rather than up to `_CATEGORY_CACHE_TTL`s
        later."""
        cache = getattr(self, "_category_cache", None)
        if cache is not None:
            cache.invalidate(self.server_id)

    def _refresh_throttle(self) -> "RefreshThrottle":
        """The throttle behind `refresh()`, created lazily so the mixin works
        when a test builds the service with `object.__new__` (skipping
        `__init__`) - same pattern as `_category_list_cache`."""
        throttle = getattr(self, "_refresh_throttle_obj", None)
        if throttle is None:
            throttle = RefreshThrottle(_REFRESH_MIN_INTERVAL)
            self._refresh_throttle_obj = throttle
        return throttle

    async def refresh(self) -> None:
        """`ConnectorInfo.refresh`: re-fetch this server's channels, roles,
        custom emoji and members from the API and write them back into
        stoat.py's cache, so a following `resolve_* / ensure_* / list_*` read
        (all cache-backed) sees an entity created since the gateway connected
        rather than missing it and letting `/mirror` spawn a duplicate (issue
        #81).

        stoat.py 1.2.1 populates the cached `Server` once at connect and only
        patches it from a narrow set of gateway events - Categories not at all
        (issue #66), and a create on another client is easy to miss - so the
        cache genuinely drifts. `/mirror` isn't a hot path, so a full
        re-fetch is fine; `RefreshThrottle` only guards the fan-out case where
        one command re-enters this many times.

        Best-effort throughout: `fetch_server` failing aborts the refresh
        *without* marking the throttle (leaving the cache untouched, the next
        attempt free to retry at once), and the member / emoji follow-up
        fetches are each guarded independently. A client whose cache isn't
        reachable (some tests) just drops the fresh data after the fetch. The
        server/channel write mirrors stoat.py's own `ServerCreateEvent`
        handling.
        """
        throttle = self._refresh_throttle()
        if not throttle.due():
            return
        try:
            server = await self._client.fetch_server(self.server_id, populate_channels=True)
        except Exception:
            logger.debug("[stoat:%s] refresh: fetch_server failed", self.connector_id, exc_info=True)
            return
        # The costly network hop landed - hold off the next full refresh now,
        # even if a follow-up fetch below fails (the cache is already mostly
        # current and re-fetching the whole server wouldn't fix a member/emoji
        # sub-fetch that's erroring).
        throttle.mark()

        # Seed the short-TTL Category cache off the same fetch so a follow-up
        # `_fresh_categories` read is served from here rather than re-fetching.
        try:
            categories = getattr(server, "categories", None)
            if categories is not None:
                self._category_list_cache().put(self.server_id, list(categories))
            else:
                self._invalidate_category_cache()
        except Exception:
            self._invalidate_category_cache()

        cache = getattr(getattr(self._client, "state", None), "cache", None)
        if cache is None:
            return
        ctx = _undefined_cache_context()
        if ctx is None:
            return
        try:
            prepare = getattr(server, "prepare_cached", None)
            for channel in (prepare() if callable(prepare) else []) or []:
                cache.store_channel(channel, ctx)
            cache.store_server(server, ctx)
        except Exception:
            logger.debug("[stoat:%s] refresh: couldn't cache server/channels", self.connector_id, exc_info=True)
            return

        try:
            members = await server.fetch_members()
            cache.overwrite_server_members(self.server_id, {str(m.id): m for m in members}, ctx)
        except Exception:
            logger.debug("[stoat:%s] refresh: couldn't refresh members", self.connector_id, exc_info=True)

        try:
            for emoji in await server.fetch_emojis():
                cache.store_emoji(emoji, ctx)
        except Exception:
            logger.debug("[stoat:%s] refresh: couldn't refresh emoji", self.connector_id, exc_info=True)

    async def resolve_category_id_by_name(self, token: str) -> str | None:
        """Resolve a bare Category title to its id (case-insensitive, first
        match); a token that's already a Category id is returned as-is, an
        unknown token yields None - this connector's
        `ConnectorInfo.resolve_category_id_by_name`. Reads the freshly-fetched
        Category list so a Category created since startup still resolves
        (issue #66)."""
        try:
            categories = await self._fresh_categories()
            if any(str(c.id) == token for c in categories):
                return token
            lowered = token.casefold()
            for c in categories:
                if str(getattr(c, "title", "")).casefold() == lowered:
                    return str(c.id)
        except Exception:
            return None
        return None

    async def ensure_category(self, name: str) -> str:
        """Get-or-create a Category titled `name`, returning its id - this
        connector's `ConnectorInfo.ensure_category` for `/mirror category`.
        Same dedicated-endpoint-then-raw-PATCH fallback as _place_in_category
        (see its docstring); the raw-PATCH list comes from `_full_category_list`
        - a fresh fetch, not the cache - so it can't revert the layout
        (issue #27)."""
        server, raw_categories = await self._full_category_list()
        lowered = name.casefold()
        existing = next(
            (c for c in raw_categories if str(c.get("title") or "").casefold() == lowered),
            None,
        )
        if existing is not None:
            return str(existing["id"])
        try:
            category = await server.create_category(name, channels=[])
            self._invalidate_category_cache()
            return str(category.id)
        except stoat.HTTPException:
            new_id = ulid_new()
            raw_categories.append({"id": new_id, "title": name, "channels": []})
            await server.state.http.request(
                stoat_routes.SERVERS_SERVER_EDIT.compile(server_id=server.id),
                json={"categories": raw_categories},
            )
            self._invalidate_category_cache()
            return str(new_id)

    async def channels_in_category(self, category_id: str) -> list[tuple[str, str]]:
        """Every channel inside Category `category_id`, as (id, name) pairs -
        this connector's `ConnectorInfo.channels_in_category`. Reads the
        freshly-fetched Category list (issue #66)."""
        try:
            categories = await self._fresh_categories()
            category = next((c for c in categories if str(c.id) == category_id), None)
        except Exception:
            return []
        if category is None:
            return []
        out: list[tuple[str, str]] = []
        for cid in getattr(category, "channels", None) or []:
            name = str(cid)
            try:
                channel = self._client.get_channel(str(cid), partial=False)
                name = getattr(channel, "name", None) or str(cid)
            except Exception:
                pass
            out.append((str(cid), name))
        return out

    async def move_channel_to_category(self, channel_id: str, category_id: str) -> None:
        """Move channel `channel_id` into Category `category_id` (removing it
        from any other Category first) - this connector's
        `ConnectorInfo.move_channel_to_category`. Raw-PATCH path, same as
        _move_channel_to_category_top (fresh-fetched list, see
        `_full_category_list` - issue #27) but appended rather than hoisted."""
        server, raw_categories = await self._full_category_list()
        target = next((c for c in raw_categories if str(c["id"]) == category_id), None)
        if target is None:
            return
        if channel_id in target["channels"]:
            return
        for c in raw_categories:
            if channel_id in c["channels"]:
                c["channels"].remove(channel_id)
        target["channels"].append(channel_id)
        await server.state.http.request(
            stoat_routes.SERVERS_SERVER_EDIT.compile(server_id=server.id),
            json={"categories": raw_categories},
        )
        self._invalidate_category_cache()

    @staticmethod
    def _roles_of(server):
        roles = getattr(server, "roles", None) or []
        return list(roles.values()) if isinstance(roles, dict) else list(roles)

    def _all_roles(self):
        server = self._client.get_server(self.server_id, partial=True)
        return self._roles_of(server)

    def _role_by_id(self, role_id: str):
        return next((r for r in self._all_roles() if str(getattr(r, "id", "")) == role_id), None)

    @staticmethod
    def _members_of(server):
        # `BaseServer.members` is a `Mapping[str, Member]` keyed by user id - a
        # plain `dict` off the cache, or `{}` when the server isn't cached
        # (`get_server(partial=True)` then hands back a bare `BaseServer`, which
        # still carries the property). Verified against stoat.py 1.2.1, not yet
        # against a live server. The `list(members)` branch is a defensive
        # fallback, mirroring `_roles_of`.
        members = getattr(server, "members", None) or []
        return list(members.values()) if isinstance(members, dict) else list(members)

    def _all_members(self):
        server = self._client.get_server(self.server_id, partial=True)
        return self._members_of(server)

    async def resolve_user_id_by_name(self, token: str) -> str | None:
        """Resolve a bare display name / nickname / username to a member id
        (case-insensitive, first match) so `/link user` etc. accept either; a
        token that's already a member id is returned as-is, an unknown token
        yields None (UserLinker then treats it as a literal id)."""
        try:
            members = self._all_members()
        except Exception:
            return None
        for member in members:
            if str(getattr(member, "id", "")) == token:
                return token
        lowered = token.casefold()
        for member in members:
            candidates = (
                getattr(member, "nick", None),
                getattr(member, "display_name", None),
                getattr(member, "name", None),
            )
            if any(c and c.casefold() == lowered for c in candidates):
                return str(member.id)
        return None

    # --- Autocomplete listing hooks (ConnectorInfo.list_*). Cache-only reads
    # off the cached server - Discord's `external_id` option autocomplete on
    # the /link etc. slash commands calls these on every keystroke, so they
    # stay on the same no-I/O `get_server(partial=True)` / `_all_*` paths the
    # bare-name resolvers use. `list_categories` is the exception - it re-fetches
    # (short-TTL-cached) because stoat.py never refreshes the cached Category
    # list from gateway events (issue #66). Each yields (id, name) pairs; an
    # uncached server yields []. ---

    async def list_channels(self) -> list[tuple[str, str]]:
        try:
            server = self._client.get_server(self.server_id, partial=True)
            channels = list(getattr(server, "channels", []) or [])
        except Exception:
            return []
        return [(str(c.id), getattr(c, "name", "") or str(c.id)) for c in channels]

    async def list_categories(self) -> list[tuple[str, str]]:
        # Unlike the other list_* hooks this re-fetches (short-TTL-cached, see
        # `_fresh_categories`): stoat.py never refreshes the cached Category
        # list from gateway events, so autocomplete would otherwise never
        # show a Category created since startup (issue #66).
        try:
            categories = await self._fresh_categories()
            return [(str(c.id), getattr(c, "title", "") or str(c.id)) for c in categories]
        except Exception:
            return []

    async def list_roles(self) -> list[tuple[str, str]]:
        try:
            roles = self._all_roles()
        except Exception:
            return []
        return [(str(r.id), getattr(r, "name", "") or str(r.id)) for r in roles]

    async def list_users(self) -> list[tuple[str, str]]:
        try:
            members = self._all_members()
        except Exception:
            return []
        out: list[tuple[str, str]] = []
        for member in members:
            name = (
                getattr(member, "nick", None)
                or getattr(member, "display_name", None)
                or getattr(member, "name", None)
                or str(member.id)
            )
            out.append((str(member.id), name))
        return out

    async def list_emotes(self) -> list[tuple[str, str]]:
        try:
            emojis = await self._all_emojis()
        except Exception:
            return []
        return [(str(e.id), getattr(e, "name", "") or str(e.id)) for e in emojis]
