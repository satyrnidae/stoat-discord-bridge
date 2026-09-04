"""`UserLinker` - `/link user`, `/unlink user`, `/linked users`."""

from __future__ import annotations

import re
import uuid

from stoat_discord_bridge.admin_commands.common import (
    ConnectorInfo,
    LinkError,
    _kick_group_member,
    _link_conflict_check,
    _require_known_connector,
    _resolve_entity_id,
    _resolve_entity_title,
    format_linked_listing,
)
from stoat_discord_bridge.storage.user_mappings import UserMapping, UserMappingRepository

# Only Discord's `/link-user` uses a real member-picker (see
# discord_service.py's _handle_link_user); Stoat's and IRC's equivalents take
# the id as free text, and a Discord id typed/pasted there commonly comes in
# as a full `<@id>`/`<@!id>` mention (e.g. copied straight out of Discord)
# rather than the bare snowflake - which then never matches a real Discord
# user and shows up unresolved (as the literal mention) in /linked-users.
# No native id on any other connector kind looks like this, so stripping it
# unconditionally here is safe regardless of which connector is involved.
_DISCORD_MENTION_RE = re.compile(r"^<@!?(\d+)>$")


def _strip_discord_mention(raw: str) -> str:
    match = _DISCORD_MENTION_RE.match(raw.strip())
    return match.group(1) if match else raw


class UserLinker:
    """`/link user` / `/unlink user` / `/linked users` - links a user's
    identity across connectors, for @mention rewriting and masquerade
    override.

    Every id argument also accepts a bare display name / username - resolved
    to an id via the connector's resolve_user_id_by_name hook, falling back
    to treating the token as an id if the hook is absent or comes up empty
    (IRC has no such hook: a user_id there already IS the nick).
    """

    def __init__(self, user_mappings: UserMappingRepository, connectors: dict[str, ConnectorInfo]) -> None:
        self._user_mappings = user_mappings
        self._connectors = connectors

    @property
    def connectors(self) -> dict[str, ConnectorInfo]:
        return self._connectors

    async def link_user(self, *, local_connector: str, local_user_id: str, source: str, source_user_id: str) -> str:
        """Link `source`'s `source_user_id` to `local_user_id` on `local_connector`.
        Raises LinkError if `source` is unknown, the two are already the same
        identity, or both already belong to two *different* existing link groups."""
        _require_known_connector(self._connectors, source)
        source_user_id = await self._resolve_to_id(source, _strip_discord_mention(source_user_id))
        local_user_id = await self._resolve_to_id(local_connector, _strip_discord_mention(local_user_id))
        source_group, local_group = await _link_conflict_check(
            self._user_mappings.get_link_group,
            source=source,
            source_id=source_user_id,
            local_connector=local_connector,
            local_id=local_user_id,
            self_link_message="can't link a user to themselves.",
            conflict_message=(
                "both users are already linked, but to different link groups - unlink one before relinking."
            ),
        )
        link_group = source_group or local_group or uuid.uuid4().hex

        await self._user_mappings.upsert(
            UserMapping(link_group=link_group, connector_id=source, user_id=source_user_id, display_name=source_user_id)
        )
        await self._user_mappings.upsert(
            UserMapping(link_group=link_group, connector_id=local_connector, user_id=local_user_id, display_name=local_user_id)
        )

        source_label = self._connectors[source].label
        local_info = self._connectors.get(local_connector)
        local_label = local_info.label if local_info else local_connector
        return f"Linked {source_label} user '{source_user_id}' to {local_label} user '{local_user_id}'."

    async def list_linked_users(self, *, local_connector: str | None = None, local_user_id: str | None = None) -> str:
        """Human-readable listing of cross-connector user links, for the
        `/linked-users` debugging command. With no target given, lists every
        link group; given a specific (local_connector, local_user_id), shows
        just that identity's group. Real display names are resolved live
        from each connector (via ConnectorInfo.resolve_user_name) rather
        than read off the stored mapping, since that's just the id it was
        linked with, never a real name (see UserMapping.display_name)."""
        if local_connector is not None and local_user_id is not None:
            local_user_id = await self._resolve_to_id(local_connector, _strip_discord_mention(local_user_id))
            link_group = await self._user_mappings.get_link_group(local_connector, local_user_id)
            if link_group is None:
                return "This user isn't linked to any others."
            groups = [await self._user_mappings.get_mapped_users(link_group)]
        else:
            groups_by_id: dict[str, list[UserMapping]] = {}
            for mapping in await self._user_mappings.get_all():
                groups_by_id.setdefault(mapping.link_group, []).append(mapping)
            if not groups_by_id:
                return "No users are linked yet."
            groups = list(groups_by_id.values())

        lines = []
        for group_mappings in groups:
            # A raw id only shown alongside the name when it adds
            # information - for IRC (whose user_id already IS the nick) or a
            # failed/unconfigured resolution, they're identical.
            parts = await format_linked_listing(
                group_mappings, self._connectors, "user_id", resolve_name=self._resolve_user_name
            )
            lines.append(" ↔ ".join(parts))
        return "Linked users:\n" + "\n".join(lines)

    async def unlink_user(self, *, local_connector: str, local_user_id: str, destination: str | None) -> str:
        """`/unlink-user`. `destination` (a connector id) kicks just that one
        identity out of `local_user_id`'s link group - everyone else
        (including this identity) stays linked to each other; None/"all"
        (the default) dissolves the whole group instead, unlinking every
        identity. Raises LinkError if the user isn't linked, or
        `destination` isn't actually a member of its group."""
        local_user_id = await self._resolve_to_id(local_connector, _strip_discord_mention(local_user_id))
        link_group = await self._user_mappings.get_link_group(local_connector, local_user_id)
        if link_group is None:
            raise LinkError("this user isn't linked to anything.")

        if destination is None or destination.lower() == "all":
            count = await self._user_mappings.delete_link_group(link_group)
            return f"Unlinked this user's entire link group ({count} identity/identities removed)."

        mapped = await self._user_mappings.get_mapped_users(link_group)
        target, _survivors = await _kick_group_member(
            mapped,
            destination,
            id_attr="user_id",
            not_a_member_message=f"'{destination}' isn't linked in this user's link group.",
            delete_mapping=self._user_mappings.delete_mapping,
        )
        label = self._connectors[destination].label if destination in self._connectors else destination
        return f"Unlinked {label} user '{target.user_id}' from this user's link group."

    async def _resolve_to_id(self, connector: str, token: str) -> str:
        """A bare display name / username -> its id via the connector's
        resolve_user_id_by_name hook; an absent/raising/empty hook (or an
        already-an-id token) leaves the token untouched."""
        info = self._connectors.get(connector)
        hook = info.resolve_user_id_by_name if info else None
        return await _resolve_entity_id(token, hook, connector=connector, kind="user")

    async def _resolve_user_name(self, connector_id: str, user_id: str) -> str:
        info = self._connectors.get(connector_id)
        hook = info.resolve_user_name if info else None
        name = await _resolve_entity_title(user_id, hook, connector=connector_id, kind="user")
        return name or user_id
