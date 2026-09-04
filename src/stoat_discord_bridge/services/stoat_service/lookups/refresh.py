"""Category-list freshness and the #66/#81 cache-refresh machinery for the
Stoat connector - split out of `categories.py` (issue #92) since it's a
distinct concern from Category *placement*: stoat.py 1.2.1 never updates its
cached `Server.categories` from gateway events, so this module re-fetches
(short-TTL-cached) rather than trusting the cache, and `refresh()` is the
throttled full-server re-fetch `/mirror` uses to see entities created since
the gateway connected. `group_parent_channel_with_threads` lives here too -
it's driven by the same cached-vs-fresh Category read as `_fresh_categories`,
re-checked on every relay.
"""

from __future__ import annotations

import logging

from stoat_discord_bridge.services.caching import AsyncTTLCache, RefreshThrottle

logger = logging.getLogger(__name__)

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


class _RefreshMixin:
    """Category-freshness / cache-refresh half of `StoatLookupsMixin`."""

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
