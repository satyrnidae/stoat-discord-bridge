"""`EmoteLinker` - `/link emote`, `/mirror emote [to|from|all]`,
`/unlink emote`, `/linked emotes`."""

from __future__ import annotations

import logging
import re
from dataclasses import replace

from stoat_discord_bridge.admin_commands.common import (
    ConnectorInfo,
    LinkError,
    MirrorGuard,
    _clean_new_name,
    _guards_mirror,
    _kick_group_member,
    _link_conflict_check,
    _mirror_all_other_connectors,
    _mirror_from_local,
    _mirror_to_destination,
    _refresh_connectors,
    _require_known_connector,
    _resolve_entity_id,
    _resolve_entity_title,
    format_linked_listing,
)
from stoat_discord_bridge.storage.emoji_mappings import EmojiMappingRepository, EmojiRef

logger = logging.getLogger(__name__)

# Emote command args commonly come in as an emoji token rather than a bare
# name/id: a `:shortcode:` (Discord/Stoat autocomplete, IRC habit) or a full
# Discord `<:name:id>` / `<a:name:id>` custom-emoji reference (pasted from a
# message). Reduce either to the bare name (or id) the resolve hooks expect.
_CUSTOM_EMOJI_RE = re.compile(r"^<a?:(\w+):(\w+)>$")
_EMOJI_SHORTCODE_RE = re.compile(r"^:([\w~+-]+):$")


def _strip_emote_token(raw: str) -> str:
    token = raw.strip()
    match = _CUSTOM_EMOJI_RE.match(token)
    if match:
        return match.group(2)
    match = _EMOJI_SHORTCODE_RE.match(token)
    if match:
        return match.group(1)
    return token


class EmoteLinker:
    """`/link emote` / `/mirror emote` / `/unlink emote` / `/linked emotes` -
    the custom-emoji counterpart of RoleLinker, backed by
    EmojiMappingRepository (the same store reaction/emoji sync uses).

    Emoji are Discord/Stoat only; IRC has no custom-emoji concept, so no
    connector there registers any emoji hook and the emote commands aren't
    offered.

    Every id argument also accepts a bare emoji NAME - resolved to an id via
    the connector's resolve_emoji_id_by_name hook, falling back to treating
    the token as an id if the hook is absent or comes up empty.
    """

    def __init__(
        self,
        emoji_mappings: EmojiMappingRepository,
        connectors: dict[str, ConnectorInfo],
        guard: MirrorGuard | None = None,
    ) -> None:
        self._emoji_mappings = emoji_mappings
        self._connectors = connectors
        self._guard = guard or MirrorGuard()

    @property
    def connectors(self) -> dict[str, ConnectorInfo]:
        return self._connectors

    async def link_emote(self, *, local_connector: str, local_id: str, source: str, source_id: str) -> str:
        """Link `source`'s emoji to a local emoji on `local_connector`. Both
        emoji arguments accept an id or a bare name. Raises LinkError if
        `source` is unknown, the two are the same emoji, or both already
        belong to two *different* existing mapping groups."""
        _require_known_connector(self._connectors, source)

        source_id = await self._resolve_to_id(source, source_id)
        local_id = await self._resolve_to_id(local_connector, local_id)
        source_group, local_group = await _link_conflict_check(
            self._emoji_mappings.get_group_id,
            source=source,
            source_id=source_id,
            local_connector=local_connector,
            local_id=local_id,
            self_link_message="can't link an emote to itself.",
            conflict_message=(
                "both emotes are already linked, but to different mapping groups - unlink one before relinking."
            ),
        )

        source_name = await self._resolve_name(source, source_id)
        local_name = await self._resolve_name(local_connector, local_id)
        source_ref = EmojiRef(connector_id=source, emoji_id=source_id, name=source_name)
        local_ref = EmojiRef(connector_id=local_connector, emoji_id=local_id, name=local_name)

        if source_group is None and local_group is None:
            group_id = await self._emoji_mappings.try_reserve(source_ref)
            if group_id is None:
                # lost a race to a concurrent reservation - fall back to whatever group now owns it
                group_id = await self._emoji_mappings.get_group_id(source, source_id)
            await self._emoji_mappings.add_refs(group_id, [local_ref])
        elif local_group is None:
            await self._emoji_mappings.add_refs(source_group, [local_ref])
        elif source_group is None:
            await self._emoji_mappings.add_refs(local_group, [source_ref])
        # else: source_group == local_group already - no-op, already linked

        source_label = self._connectors[source].label
        local_info = self._connectors.get(local_connector)
        local_label = local_info.label if local_info else local_connector
        return f"Linked {source_label} emote '{source_name}' to {local_label} emote '{local_name}'."

    @_guards_mirror(_mirror_to_destination)
    async def mirror_emote(
        self, *, local_connector: str, local_emote: str, destination: str, new_name: str | None = None
    ) -> str:
        """Ensure `local_emote` (id or bare name, on `local_connector`) has a
        linked counterpart on `destination`: reuses the existing link if the
        pair is already linked, otherwise reads the source emoji's image via
        `local_connector`'s resolve_emoji hook, recreates it on `destination`
        via its ensure_emoji hook, and links the two. Reports rather than
        raises per problem so `mirror_emote_all` can carry on past one bad
        destination.

        `new_name`, if given, is the name the counterpart emoji is
        created/matched under on `destination` instead of the source emoji's
        name (issue #44)."""
        _require_known_connector(self._connectors, destination)
        if destination == local_connector:
            raise LinkError("can't mirror an emote to its own connector.")

        await _refresh_connectors(self._connectors, local_connector, destination)

        source_id = await self._resolve_to_id(local_connector, local_emote)
        source_name = await self._resolve_name(local_connector, source_id)
        target_name = _clean_new_name(new_name) or source_name
        dest_info = self._connectors[destination]

        group_id = await self._emoji_mappings.get_group_id(local_connector, source_id)
        if group_id is not None:
            refs = await self._emoji_mappings.get_refs(group_id)
            if any(r.connector_id == destination for r in refs):
                return f"{dest_info.label}: already synced - skipped."

        # Prefer linking to a same-named emote that already exists on the
        # destination over creating a duplicate (mirrors /mirror role's
        # create-or-match). Name only - we can't compare images.
        if dest_info.resolve_emoji_id_by_name is not None and target_name:
            try:
                existing_id = await dest_info.resolve_emoji_id_by_name(target_name)
            except Exception:
                logger.debug("mirror-emote: %s.resolve_emoji_id_by_name(%r) failed", destination, target_name, exc_info=True)
                existing_id = None
            if existing_id:
                try:
                    return await self.link_emote(
                        local_connector=destination, local_id=existing_id, source=local_connector, source_id=source_id
                    )
                except LinkError as exc:
                    return f"{dest_info.label}: {exc}"

        source_info = self._connectors.get(local_connector)
        if source_info is None or source_info.resolve_emoji is None:
            return f"{dest_info.label}: can't read {local_connector}'s emoji to copy it."
        if dest_info.ensure_emoji is None:
            return f"{dest_info.label}: doesn't support emoji creation - link it manually with /link emote."

        try:
            custom_emoji = await source_info.resolve_emoji(source_id)
        except Exception as exc:
            logger.warning("mirror-emote: %s.resolve_emoji(%r) failed: %s", local_connector, source_id, exc)
            return f"{dest_info.label}: couldn't read the source emoji: {exc}"
        if custom_emoji is None:
            return f"{dest_info.label}: source emoji '{source_name}' not found."
        if target_name != custom_emoji.name:
            # An emoji can't be created name-only (unlike a role) - hand
            # ensure_emoji the same CustomEmoji with just the name swapped so
            # the recreated copy takes `new_name` (issue #44).
            custom_emoji = replace(custom_emoji, name=target_name)

        try:
            created = await dest_info.ensure_emoji(custom_emoji)
        except Exception as exc:
            logger.warning("mirror-emote: %s.ensure_emoji(%r) failed: %s", destination, target_name, exc)
            return f"{dest_info.label}: failed to create the emoji: {exc}"
        if created is None:
            return f"{dest_info.label}: couldn't create the emoji (slots full, name rejected, image too large?)."

        try:
            return await self.link_emote(
                local_connector=destination, local_id=created.native_id, source=local_connector, source_id=source_id
            )
        except LinkError as exc:
            return f"{dest_info.label}: {exc}"

    @_guards_mirror(_mirror_all_other_connectors)
    async def mirror_emote_all(self, *, local_connector: str, local_emote: str) -> str:
        """`/mirror emote <local> all` - mirror_emote() against every other
        configured connector, one line of summary/skip/error per connector.
        Reserves every destination up front, so a single busy one rejects the
        whole fan-out with that connector named (issue #79)."""
        results = [
            await self.mirror_emote(
                local_connector=local_connector, local_emote=local_emote, destination=destination
            )
            for destination in self._connectors
            if destination != local_connector
        ]
        return "\n".join(r for r in results if r) if results else "no other connectors configured."

    @_guards_mirror(_mirror_from_local)
    async def mirror_emote_from(
        self, *, local_connector: str, source: str, source_emote: str, new_name: str | None = None
    ) -> str:
        """`/mirror emote from <source> <source_emote>` - read `source`'s
        emoji and recreate-or-match it *here* on `local_connector`, then link
        the two. `mirror_emote` with the connectors swapped.

        `new_name`, if given, names the local counterpart emoji instead of
        carrying the source emoji's name over (issue #44)."""
        _require_known_connector(self._connectors, source)
        if source == local_connector:
            raise LinkError("can't mirror an emote from a connector to itself.")
        return await self.mirror_emote(
            local_connector=source, local_emote=source_emote, destination=local_connector, new_name=new_name
        )

    async def list_linked_emotes(
        self, *, local_connector: str, local_emote: str | None = None, service: str | None = None
    ) -> str:
        """Read-only listing, for `/linked emotes` - never raises LinkError.
        With a `local_emote`, shows just that emoji's group; without one (or
        with `service == "all"`), lists every group."""
        if local_emote is not None and (service is None or service.lower() != "all"):
            local_id = await self._resolve_to_id(local_connector, local_emote)
            group_id = await self._emoji_mappings.get_group_id(local_connector, local_id)
            if group_id is None:
                return "This emote isn't linked to any others."
            groups = [await self._emoji_mappings.get_refs(group_id)]
        else:
            all_groups = await self._emoji_mappings.get_all_groups()
            if not all_groups:
                return "No emotes are linked yet."
            groups = list(all_groups.values())

        lines = []
        for refs in groups:
            parts = await format_linked_listing(refs, self._connectors, "emoji_id", resolve_name=self._resolve_name)
            lines.append(" ↔ ".join(parts))
        return "Linked emotes:\n" + "\n".join(lines)

    async def unlink_emote(self, *, local_connector: str, local_emote: str, destination: str | None) -> str:
        """`/unlink emote`. `destination` (a connector id) kicks just that one
        member out of the emoji's mapping group; None/"all" (the default)
        dissolves the whole group. A kick that would strand a lone survivor
        dissolves the group instead (a group of one isn't a bridge)."""
        local_id = await self._resolve_to_id(local_connector, local_emote)
        group_id = await self._emoji_mappings.get_group_id(local_connector, local_id)
        if group_id is None:
            raise LinkError("this emote isn't linked to anything.")

        if destination is None or destination.lower() == "all":
            count = await self._emoji_mappings.delete_group(group_id)
            return f"Unlinked this emote's entire mapping group ({count} emote(s) removed)."

        refs = await self._emoji_mappings.get_refs(group_id)

        async def _dissolve(survivors: list[EmojiRef]) -> None:
            await self._emoji_mappings.delete_group(group_id)

        target, _survivors = await _kick_group_member(
            refs,
            destination,
            id_attr="emoji_id",
            not_a_member_message=f"'{destination}' isn't linked in this emote's mapping group.",
            delete_mapping=self._emoji_mappings.delete_ref,
            dissolve_survivors=_dissolve,
        )
        label = self._connectors[destination].label if destination in self._connectors else destination
        return f"Unlinked {label} emote '{target.name}' ({target.emoji_id}) from this mapping group."

    async def _resolve_to_id(self, connector: str, token: str) -> str:
        token = _strip_emote_token(token)
        info = self._connectors.get(connector)
        hook = info.resolve_emoji_id_by_name if info else None
        return await _resolve_entity_id(token, hook, connector=connector, kind="emoji")

    async def _resolve_name(self, connector_id: str, emoji_id: str) -> str:
        info = self._connectors.get(connector_id)
        hook = info.resolve_emoji_name if info else None
        name = await _resolve_entity_title(emoji_id, hook, connector=connector_id, kind="emoji")
        return name or emoji_id
