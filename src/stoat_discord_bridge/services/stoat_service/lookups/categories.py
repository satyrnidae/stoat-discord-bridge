"""Category placement for the Stoat connector: getting a channel into a
Category (creating it if needed) via the dedicated categories endpoint or -
since every deployment tested 404s that - a raw whole-server PATCH, plus
Category get-or-create/move/list for `/mirror category` and `/link category`.
Category-list *freshness* and the #66/#81 cache-refresh machinery this module
reads through (`_fresh_categories`, `refresh()`, `group_parent_channel_with_threads`)
live in the sibling `refresh.py` - split out (issue #92) so this module,
which was the largest single concern in the old monolithic `lookups.py`,
stays smaller than discord's `lookups.py`.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace

import stoat
from stoat import routes as stoat_routes
from stoat.core import ulid_new

logger = logging.getLogger(__name__)


class _CategoriesMixin:
    """Category-placement half of `StoatLookupsMixin`."""

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
