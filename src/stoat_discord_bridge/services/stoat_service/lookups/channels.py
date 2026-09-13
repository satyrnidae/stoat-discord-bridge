"""Channel get-or-create for the Stoat connector - `ensure_channel` /
`describe_channel` / `_apply_channel_icon` - the `ConnectorInfo.ensure_channel`
/ `ConnectorInfo.describe_channel` hooks behind `/mirror channel` and Discord
thread mirroring. Category *placement* (as opposed to creating the channel
itself) lives in `categories.py` - `ensure_channel` calls into it via
`self._ensure_channel_in_category` when a `category` is given.
"""

from __future__ import annotations

import logging

import stoat
from stoat import routes as stoat_routes

from stoat_discord_bridge.models import ChannelMetadata
from stoat_discord_bridge.services.stoat_service.formatting import _download

logger = logging.getLogger(__name__)

# Stoat channel descriptions are capped at 1024 chars (stoat.py
# Server.create_channel); a longer source description is clipped rather than
# rejected.
_DESCRIPTION_LIMIT = 1024


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


class _ChannelsMixin:
    """Channel get-or-create half of `StoatLookupsMixin`."""

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
        flag / icon / slowmode delay - applied *only when this call creates
        the channel* (issue #32, #108); a mirror that matched an existing
        channel leaves its metadata untouched. The icon and slowmode are
        each a best-effort follow-up that never blocks the create from
        succeeding."""
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
            if metadata is not None and metadata.slowmode_delay:
                await self._apply_channel_slowmode(server, channel_id, metadata.slowmode_delay)
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
        / slowmode delay as a `ChannelMetadata`, this connector's
        `ConnectorInfo.describe_channel` - `/mirror channel` reads it off the
        source channel so the mirrored copy isn't left blank (issue #32,
        #108). description/NSFW/icon are cache-only (same `partial=False`
        pattern as `get_channel_name`); None if the channel isn't resolvable.
        Slowmode isn't on the cached object at all (stoat.py's typed client
        drops the field), so it's read via a separate best-effort raw fetch -
        see `_fetch_channel_slowmode`."""
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
            slowmode_delay=await self._fetch_channel_slowmode(channel_id),
        )

    async def _fetch_channel_slowmode(self, channel_id: str) -> int | None:
        """Best-effort raw fetch of a channel's slowmode delay, in seconds -
        stoat.py 1.2.1's typed `Channel` doesn't model the field (it drops
        unknown payload keys), so `describe_channel`'s cached object never
        carries it (issue #108). `describe_channel` isn't a hot path (admin
        commands + Discord thread mirroring only), so an extra round-trip
        here is acceptable - same rationale `ensure_channel`'s fresh-server-
        fetch already uses. None on any failure, or when the channel has no
        slowmode set."""
        try:
            data = await self._client.http.request(
                stoat_routes.CHANNELS_CHANNEL_FETCH.compile(channel_id=channel_id)
            )
        except Exception:
            return None
        return data.get("slowmode") or None

    async def _apply_channel_slowmode(self, server, channel_id: str, slowmode_delay: int) -> None:
        """Best-effort: PATCH the newly-created channel's slowmode via a raw
        HTTP call - stoat.py's typed client has no method for it (issue
        #108), same raw-HTTP-fallback pattern `_place_via_server_edit` uses
        for Category placement. Only reached from `ensure_channel`'s create
        path; never raises - a mirrored channel with no slowmode set is
        still a working channel."""
        try:
            await server.state.http.request(
                stoat_routes.CHANNELS_CHANNEL_EDIT.compile(channel_id=channel_id),
                json={"slowmode": slowmode_delay},
            )
        except Exception:
            logger.warning(
                "[stoat:%s] couldn't set mirrored channel %s slowmode to %s",
                self.connector_id,
                channel_id,
                slowmode_delay,
                exc_info=True,
            )
