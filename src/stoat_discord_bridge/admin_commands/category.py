"""`CategoryLinker` - `/link category`, `/mirror category [to|from|all]`,
`/unlink category`, `/linked categories`, and the auto-sync side effect of
linking two Categories (a new channel in either auto-mirrors into the
other's linked Category), plus the thread-Category binding backing Discord
thread mirroring (see `stoat_discord_bridge.services.discord_service`)."""

from __future__ import annotations

import logging
import uuid

from stoat_discord_bridge.admin_commands.channel import ChannelLinker
from stoat_discord_bridge.admin_commands.common import (
    ConnectorInfo,
    LinkError,
    MirrorGuard,
    MirrorInProgressError,
    _clean_new_name,
    _group_conflict_check,
    _guards_mirror,
    _kick_group_member,
    _mirror_all_other_connectors,
    _mirror_from_local,
    _mirror_to_destination,
    _refresh_connectors,
    _reject_self_link,
    _require_known_connector,
    _resolve_entity_id,
    _resolve_entity_title,
    format_linked_listing,
)
from stoat_discord_bridge.storage.category_mappings import (
    CategoryMapping,
    CategoryMappingRepository,
    ThreadCategoryRepository,
)

logger = logging.getLogger(__name__)


class CategoryLinker:
    """The Category-level counterpart of ChannelLinker: `/link category`
    links two Categories across connectors, and once linked, a new channel
    appearing inside either is auto-synced (created + linked) onto the
    other's own linked Category - see sync_new_channel, called from each
    connector's own channel-create event handler
    (DiscordSenderService._handle_channel_create,
    StoatSenderService._handle_channel_create)."""

    def __init__(
        self,
        category_mappings: CategoryMappingRepository,
        thread_categories: ThreadCategoryRepository,
        channel_linker: ChannelLinker,
        connectors: dict[str, ConnectorInfo],
        guard: MirrorGuard | None = None,
    ) -> None:
        self._category_mappings = category_mappings
        self._thread_categories = thread_categories
        self._channel_linker = channel_linker
        self._connectors = connectors
        # Falls back to the ChannelLinker's guard so a bare
        # CategoryLinker(... channel_linker ...) in tests still shares one
        # guard with the child-channel mirrors it delegates.
        self._guard = guard or channel_linker._guard

    @property
    def connectors(self) -> dict[str, ConnectorInfo]:
        return self._connectors

    async def link_category(
        self,
        *,
        local_connector: str,
        local_category_id: str | None,
        local_category_name: str,
        source: str,
        source_id: str,
        destination_id: str | None,
    ) -> str:
        """Link `source`'s `source_id` Category to `destination_id` (or the
        invoking Category, if omitted) on `local_connector`. Symmetric to
        ChannelLinker.link_channel, plus a guard rejecting either side if
        it's a Category Discord's thread/forum-post auto-mirroring created
        (see DiscordSenderService._handle_thread_create) - such a Category
        is dedicated to that per-thread-parent mirroring flow, and linking
        it here would create a second, conflicting sync path onto the same
        channels. Once linked, any new channel that appears in either
        Category is auto-synced onto the other - see sync_new_channel."""
        _require_known_connector(self._connectors, source)

        source_id = await self._resolve_to_id(source, source_id)
        if destination_id is not None:
            destination_id = await self._resolve_to_id(local_connector, destination_id)
        if destination_id is None and local_category_id is None:
            raise LinkError("this channel isn't inside a Category.")

        if destination_id is None or destination_id == local_category_id:
            destination_category_id = local_category_id
            destination_name = local_category_name
        else:
            destination_category_id = destination_id
            destination_name = await self._resolve_name(local_connector, destination_id)

        _reject_self_link(
            source=source,
            source_id=source_id,
            local_connector=local_connector,
            local_id=destination_category_id,
            message="can't link a Category to itself.",
        )

        if await self._thread_categories.is_thread_category(source, source_id) or await self._thread_categories.is_thread_category(
            local_connector, destination_category_id
        ):
            raise LinkError(
                "that Category was auto-created for Discord thread mirroring and can't be linked with /link category."
            )

        source_group, destination_group = await _group_conflict_check(
            self._category_mappings.get_bridge_group,
            source=source,
            source_id=source_id,
            local_connector=local_connector,
            local_id=destination_category_id,
            conflict_message=(
                "both Categories are already linked, but to different bridge groups - unlink one before relinking."
            ),
        )
        bridge_group = source_group or destination_group or uuid.uuid4().hex

        source_name = await self._resolve_name(source, source_id)
        await self._category_mappings.upsert(
            CategoryMapping(bridge_group=bridge_group, connector_id=source, category_id=source_id, category_name=source_name)
        )
        await self._category_mappings.upsert(
            CategoryMapping(
                bridge_group=bridge_group,
                connector_id=local_connector,
                category_id=destination_category_id,
                category_name=destination_name,
            )
        )

        source_label = self._connectors[source].label
        local_info = self._connectors.get(local_connector)
        local_label = local_info.label if local_info else local_connector
        return (
            f"Linked {source_label} Category '{source_name}' ({source_id}) to "
            f"{local_label} Category '{destination_name}' ({destination_category_id}). "
            "New channels in either will now sync automatically."
        )

    async def list_linked_categories(
        self, *, local_connector: str, local_category_id: str | None = None, local_category: str | None = None
    ) -> str:
        """Read-only listing, for `/linked categories` - never raises
        LinkError, same as ChannelLinker.list_linked_channels. `local_category`
        (an id or a bare name) overrides `local_category_id` (the invoking
        channel's Category) when given."""
        if local_category is not None:
            local_category_id = await self._resolve_to_id(local_connector, local_category)
        if local_category_id is None:
            return "This channel isn't inside a Category."
        bridge_group = await self._category_mappings.get_bridge_group(local_connector, local_category_id)
        if bridge_group is None:
            return "This Category isn't linked to any others."

        mapped = await self._category_mappings.get_mapped_categories(bridge_group)
        lines = await format_linked_listing(
            mapped,
            self._connectors,
            "category_id",
            "category_name",
            marker_for=(local_connector, local_category_id),
            marker_text=" (this Category)",
        )
        return "Linked Categories:\n" + "\n".join(lines)

    async def unlink_category(
        self,
        *,
        local_connector: str,
        local_category_id: str | None = None,
        local_category: str | None = None,
        destination: str | None,
    ) -> str:
        """`/unlink category`, symmetric to ChannelLinker.unlink_channel.
        `local_category` (an id or a bare name) overrides `local_category_id`
        (the invoking channel's Category) when given."""
        if local_category is not None:
            local_category_id = await self._resolve_to_id(local_connector, local_category)
        if local_category_id is None:
            raise LinkError("this channel isn't inside a Category.")
        bridge_group = await self._category_mappings.get_bridge_group(local_connector, local_category_id)
        if bridge_group is None:
            raise LinkError("this Category isn't linked to anything.")

        if destination is None or destination.lower() == "all":
            count = await self._category_mappings.delete_bridge_group(bridge_group)
            return f"Unlinked this Category's entire bridge group ({count} Category(s) removed)."

        mapped = await self._category_mappings.get_mapped_categories(bridge_group)
        target, _survivors = await _kick_group_member(
            mapped,
            destination,
            id_attr="category_id",
            not_a_member_message=f"'{destination}' isn't linked in this Category's bridge group.",
            delete_mapping=self._category_mappings.delete_mapping,
        )
        label = self._connectors[destination].label if destination in self._connectors else destination
        return f"Unlinked {label} Category '{target.category_name}' ({target.category_id}) from this bridge group."

    @_guards_mirror(_mirror_to_destination)
    async def mirror_category(
        self,
        *,
        local_connector: str,
        local_category_id: str | None = None,
        local_category: str | None = None,
        local_category_name: str | None = None,
        destination: str,
        new_name: str | None = None,
    ) -> str:
        """Ensure `local_category` (id or bare name, on `local_connector`) has
        a linked counterpart Category on `destination`: reuses the existing
        linked Category there if the pair is already linked, otherwise creates
        a same-named one via `destination`'s ensure_category() hook and links
        it. Then relocates every channel inside the source Category onto that
        destination Category - a child already linked to a `destination`
        channel is *moved* into it (move_channel_to_category hook), an
        unlinked child is mirrored (created + linked) there via
        ChannelLinker.mirror_channel. Reports rather than raises per problem so
        `mirror_category_all` can carry on past one bad destination.

        `new_name`, if given, is the title to create/find the counterpart
        Category under on `destination` instead of the source Category's title
        (issue #44); it doesn't rename any mirrored child channels."""
        _require_known_connector(self._connectors, destination)
        if destination == local_connector:
            raise LinkError("can't mirror a Category to its own connector.")

        await _refresh_connectors(self._connectors, local_connector, destination)

        if local_category is not None:
            local_category_id = await self._resolve_to_id(local_connector, local_category)
        if local_category_id is None:
            raise LinkError("this channel isn't inside a Category.")
        source_name = local_category_name or await self._resolve_name(local_connector, local_category_id)
        target_name = _clean_new_name(new_name) or source_name

        dest_info = self._connectors[destination]
        dest_label = dest_info.label

        bridge_group = await self._category_mappings.get_bridge_group(local_connector, local_category_id)
        dest_category_id: str | None = None
        match = None
        if bridge_group is not None:
            existing = await self._category_mappings.get_mapped_categories(bridge_group)
            match = next((m for m in existing if m.connector_id == destination), None)
            if match is not None:
                dest_category_id = match.category_id

        # Resolve the destination Category's name, but fall back to a known-good
        # `fallback` rather than echoing the raw id when the lookup comes up
        # empty. `ensure_category` just created-or-matched the Category as
        # `target_name`, and the connector's cache won't show a brand-new one
        # yet, so `_resolve_name` would hand back the id - which then gets
        # stored as the Category's name and, worse, passed to child-channel
        # placement as a Category *title*, spawning a second Category literally
        # named after the id (issue #64).
        async def _dest_name(fallback: str) -> str:
            resolved = await self._resolve_name(destination, dest_category_id)
            return resolved if resolved != dest_category_id else fallback

        lines: list[str] = []
        if dest_category_id is None:
            if dest_info.ensure_category is None:
                return f"{dest_label}: doesn't support Category creation - link it manually with /link category."
            try:
                dest_category_id = await dest_info.ensure_category(target_name)
            except Exception as exc:
                logger.warning("mirror-category: %s.ensure_category(%r) failed: %s", destination, target_name, exc)
                return f"{dest_label}: failed to create/find a Category: {exc}"
            try:
                lines.append(
                    await self.link_category(
                        local_connector=destination,
                        local_category_id=dest_category_id,
                        local_category_name=await _dest_name(target_name),
                        source=local_connector,
                        source_id=local_category_id,
                        destination_id=None,
                    )
                )
            except LinkError as exc:
                return f"{dest_label}: {exc}"
        else:
            lines.append(f"{dest_label}: already linked - reusing '{dest_category_id}'.")

        dest_category_name = await _dest_name(
            (match.category_name if match is not None else None) or target_name
        )
        info = self._connectors.get(local_connector)
        if info is not None and info.channels_in_category is not None:
            try:
                children = await info.channels_in_category(local_category_id)
            except Exception:
                logger.debug("mirror-category: channels_in_category failed for %s", local_connector, exc_info=True)
                children = []
            for cid, cname in children:
                try:
                    linked = await self._channel_linker._linked_channel(local_connector, cid, destination)
                    if linked is not None and dest_info.move_channel_to_category is not None:
                        await dest_info.move_channel_to_category(linked.channel_id, dest_category_id)
                        lines.append(f"{dest_label}: moved '{cname}' into the Category.")
                    else:
                        lines.append(
                            await self._channel_linker.mirror_channel(
                                local_connector=local_connector,
                                local_channel_id=cid,
                                local_channel_name=cname,
                                destination=destination,
                                local_channel_category=dest_category_name,
                            )
                        )
                except Exception as exc:
                    logger.warning("mirror-category: child %r -> %s failed: %s", cname, destination, exc)
                    lines.append(f"{dest_label}: '{cname}' failed: {exc}")
        return "\n".join(lines)

    @_guards_mirror(_mirror_all_other_connectors)
    async def mirror_category_all(
        self,
        *,
        local_connector: str,
        local_category_id: str | None = None,
        local_category: str | None = None,
        local_category_name: str | None = None,
    ) -> str:
        """`/mirror category <local> all` - mirror_category() against every
        other configured connector. Reserves every destination up front, so a
        single busy one rejects the whole fan-out with that connector named
        (issue #79)."""
        results = [
            await self.mirror_category(
                local_connector=local_connector,
                local_category_id=local_category_id,
                local_category=local_category,
                local_category_name=local_category_name,
                destination=destination,
            )
            for destination in self._connectors
            if destination != local_connector
        ]
        return "\n".join(r for r in results if r) if results else "no other connectors configured."

    @_guards_mirror(_mirror_from_local)
    async def mirror_category_from(
        self, *, local_connector: str, source: str, source_id: str, new_name: str | None = None
    ) -> str:
        """`/mirror category from <source> <source_id>` - `source`'s Category
        already exists; create a linked counterpart *here* on
        `local_connector`, link them, and relocate/mirror the source
        Category's channels into it. `mirror_category` with the connectors
        swapped.

        `new_name`, if given, titles the local counterpart Category instead of
        carrying the source Category's title over (issue #44)."""
        _require_known_connector(self._connectors, source)
        if source == local_connector:
            raise LinkError("can't mirror a Category from a connector to itself.")
        return await self.mirror_category(
            local_connector=source, local_category=source_id, destination=local_connector, new_name=new_name
        )

    async def sync_new_channel(
        self, *, local_connector: str, local_category_id: str, channel_id: str, channel_name: str
    ) -> None:
        """Called by each connector's channel-create event handler when a
        new channel appears inside `local_category_id`. If that Category is
        linked (via /link-category), auto-mirrors the new channel onto every
        other connector in its bridge group - into that destination's own
        linked Category (by name), not `local_category_id`'s name, since
        /link-category allows differently-named Categories across
        connectors (unlike /mirror channel's same-name carry-over). No-op if
        the Category isn't linked - which a thread-mirroring-created
        Category never is, since link_category refuses to ever link one, so
        this is naturally never triggered for those without needing its own
        explicit guard. Reuses ChannelLinker.mirror_channel, whose own
        "already synced - skipped" check makes this safe against duplicate/
        echoed channel-create events (e.g. the bridge's own created channel
        firing its creator's event back at this same listener)."""
        bridge_group = await self._category_mappings.get_bridge_group(local_connector, local_category_id)
        if bridge_group is None:
            return
        mapped = await self._category_mappings.get_mapped_categories(bridge_group)
        for mapping in mapped:
            if mapping.connector_id == local_connector:
                continue
            try:
                result = await self._channel_linker.mirror_channel(
                    local_connector=local_connector,
                    local_channel_id=channel_id,
                    local_channel_name=channel_name,
                    destination=mapping.connector_id,
                    local_channel_category=mapping.category_name,
                )
            except MirrorInProgressError:
                # A manual `/mirror` into this destination is running - it'll
                # pick this channel up itself if it's a child of the mirrored
                # Category; otherwise the operator can re-run. Don't race it.
                logger.info(
                    "[category-sync] new channel %r -> %s deferred: a /mirror into it is in progress",
                    channel_name,
                    mapping.connector_id,
                )
                continue
            logger.info(
                "[category-sync] new channel %r in %s's linked Category -> %s: %s",
                channel_name,
                local_connector,
                mapping.connector_id,
                result,
            )

    async def bind_thread_category(
        self, connector_id: str, parent_channel_id: str, category_id: str
    ) -> None:
        """Bind `parent_channel_id` (on `connector_id`) to the Stoat Category
        `category_id` auto-created for its Discord threads - called from a
        connector's ensure_channel() when it was itself called with
        is_thread_category=True (ultimately from
        DiscordSenderService._handle_thread_create). Later threads for the
        same parent resolve the Category by this id, not by title."""
        await self._thread_categories.bind(connector_id, parent_channel_id, category_id)

    async def thread_category_id(self, connector_id: str, parent_channel_id: str) -> str | None:
        """The Category id bound to `parent_channel_id` on `connector_id`, or
        None if no thread Category has been created for it yet."""
        return await self._thread_categories.get_category_id(connector_id, parent_channel_id)

    async def thread_category_parent(self, connector_id: str, category_id: str) -> str | None:
        """The parent channel id bound to thread Category `category_id` on
        `connector_id` - the reverse lookup, used to group the parent channel
        into its thread Category by id rather than by name match."""
        return await self._thread_categories.get_parent_channel_id(connector_id, category_id)

    async def forget_thread_category(self, connector_id: str, parent_channel_id: str) -> None:
        """Drop `parent_channel_id`'s binding on `connector_id` - its bound
        Category is gone from the server, so the next thread rebinds it."""
        await self._thread_categories.forget(connector_id, parent_channel_id)

    async def is_thread_category(self, connector_id: str, category_id: str) -> bool:
        """Whether `category_id` on `connector_id` was auto-created for
        Discord thread/forum-post mirroring - the read side of
        bind_thread_category, used by `/link-category` to refuse linking it
        and by StoatSenderService to decide whether to group a thread
        Category's parent channel into it."""
        return await self._thread_categories.is_thread_category(connector_id, category_id)

    async def _resolve_to_id(self, connector: str, token: str) -> str:
        info = self._connectors.get(connector)
        hook = info.resolve_category_id_by_name if info else None
        return await _resolve_entity_id(token, hook, connector=connector, kind="category")

    async def _resolve_name(self, connector_id: str, category_id: str) -> str:
        info = self._connectors.get(connector_id)
        hook = info.resolve_category_name if info else None
        title = await _resolve_entity_title(category_id, hook, connector=connector_id, kind="category")
        return title or category_id
