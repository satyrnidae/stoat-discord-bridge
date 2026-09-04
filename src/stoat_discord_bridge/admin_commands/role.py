"""`RoleLinker` - `/link role`, `/mirror role [to|from|all]`, `/unlink role`,
`/linked roles`."""

from __future__ import annotations

import logging
import uuid

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
from stoat_discord_bridge.storage.role_mappings import RoleMapping, RoleMappingRepository

logger = logging.getLogger(__name__)


class RoleLinker:
    """`/link role` / `/mirror role` / `/unlink role` / `/linked roles` - the
    role-level counterpart of ChannelLinker, modeled on UserLinker (for the
    list/unlink/name-resolution shape) and ChannelLinker.mirror_channel (for
    the ensure-then-link, report-don't-raise-per-destination shape).

    Roles are Discord/Stoat only; IRC has no role concept, so no connector
    there registers any of the role hooks and `/link role` isn't offered.

    Every id argument also accepts a bare role NAME - resolved to an id via
    the connector's resolve_role_id_by_name hook, falling back to treating
    the token as an id if the hook is absent or comes up empty.
    """

    def __init__(
        self,
        role_mappings: RoleMappingRepository,
        connectors: dict[str, ConnectorInfo],
        guard: MirrorGuard | None = None,
    ) -> None:
        self._role_mappings = role_mappings
        self._connectors = connectors
        self._guard = guard or MirrorGuard()

    @property
    def connectors(self) -> dict[str, ConnectorInfo]:
        return self._connectors

    async def link_role(
        self,
        *,
        local_connector: str,
        local_role: str,
        source: str,
        source_role: str,
        destination_role: str | None = None,
    ) -> str:
        """Link `source`'s `source_role` to `destination_role` (or
        `local_role`) on `local_connector`. Both role arguments accept an id
        or a bare name. Raises LinkError if `source` is unknown, the two are
        the same role, or both are already linked to two different bridge
        groups."""
        _require_known_connector(self._connectors, source)

        source_id = await self._resolve_to_id(source, source_role)
        local_id = await self._resolve_to_id(local_connector, destination_role or local_role)

        source_group, local_group = await _link_conflict_check(
            self._role_mappings.get_bridge_group,
            source=source,
            source_id=source_id,
            local_connector=local_connector,
            local_id=local_id,
            self_link_message="can't link a role to itself.",
            conflict_message=(
                "both roles are already linked, but to different bridge groups - unlink one before relinking."
            ),
        )
        bridge_group = source_group or local_group or uuid.uuid4().hex

        source_name = await self._resolve_name(source, source_id)
        local_name = await self._resolve_name(local_connector, local_id)
        await self._role_mappings.upsert(
            RoleMapping(bridge_group=bridge_group, connector_id=source, role_id=source_id, role_name=source_name)
        )
        await self._role_mappings.upsert(
            RoleMapping(
                bridge_group=bridge_group, connector_id=local_connector, role_id=local_id, role_name=local_name
            )
        )

        source_label = self._connectors[source].label
        local_info = self._connectors.get(local_connector)
        local_label = local_info.label if local_info else local_connector
        return (
            f"Linked {source_label} role '{source_name}' ({source_id}) to "
            f"{local_label} role '{local_name}' ({local_id})."
        )

    @_guards_mirror(_mirror_to_destination)
    async def mirror_role(
        self, *, local_connector: str, local_role: str, destination: str, new_name: str | None = None
    ) -> str:
        """Ensure `local_role` (on `local_connector`) has a linked
        counterpart on `destination`: reuses/creates a same-named role there
        via `destination`'s ensure_role() hook, then links it. Reports rather
        than raises for an already-synced pair, a destination that can't
        create roles, or a link conflict - the bulk `mirror_role_all` caller
        shouldn't have one bad destination abort the rest.

        `new_name`, if given, is the name to create/find the counterpart role
        under on `destination` instead of the source role's name (issue #44)."""
        _require_known_connector(self._connectors, destination)
        if destination == local_connector:
            raise LinkError("can't mirror a role to its own connector.")

        await _refresh_connectors(self._connectors, local_connector, destination)

        local_id = await self._resolve_to_id(local_connector, local_role)
        local_name = await self._resolve_name(local_connector, local_id)
        target_name = _clean_new_name(new_name) or local_name

        bridge_group = await self._role_mappings.get_bridge_group(local_connector, local_id)
        if bridge_group is not None:
            existing = await self._role_mappings.get_mapped_roles(bridge_group)
            if any(m.connector_id == destination for m in existing):
                return f"{self._connectors[destination].label}: already synced - skipped."

        dest_info = self._connectors[destination]
        if dest_info.ensure_role is None:
            return f"{dest_info.label}: doesn't support role creation - link it manually with /link role."

        try:
            destination_role_id = await dest_info.ensure_role(target_name)
        except Exception as exc:
            logger.warning("mirror-role: %s.ensure_role(%r) failed: %s", destination, target_name, exc)
            return f"{dest_info.label}: failed to create/find a role: {exc}"

        try:
            return await self.link_role(
                local_connector=destination,
                local_role=destination_role_id,
                source=local_connector,
                source_role=local_id,
            )
        except LinkError as exc:
            return f"{dest_info.label}: {exc}"

    @_guards_mirror(_mirror_all_other_connectors)
    async def mirror_role_all(self, *, local_connector: str, local_role: str) -> str:
        """`/mirror role <local> all` - mirror_role() against every other
        configured connector, one line of summary/skip/error per connector.
        Reserves every destination up front, so a single busy one rejects the
        whole fan-out with that connector named (issue #79)."""
        results = [
            await self.mirror_role(
                local_connector=local_connector, local_role=local_role, destination=destination
            )
            for destination in self._connectors
            if destination != local_connector
        ]
        return "\n".join(results) if results else "no other connectors configured."

    @_guards_mirror(_mirror_from_local)
    async def mirror_role_from(
        self, *, local_connector: str, source: str, source_role: str, new_name: str | None = None
    ) -> str:
        """`/mirror role from <source> <source_role>` - `source`'s role
        already exists; create-or-match a linked counterpart *here* on
        `local_connector` and link them. `mirror_role` with the connectors
        swapped, so bridge-group reuse (via `link_role`) comes for free.

        `new_name`, if given, names the local counterpart role instead of
        carrying the source role's name over (issue #44)."""
        _require_known_connector(self._connectors, source)
        if source == local_connector:
            raise LinkError("can't mirror a role from a connector to itself.")
        return await self.mirror_role(
            local_connector=source, local_role=source_role, destination=local_connector, new_name=new_name
        )

    async def list_linked_roles(
        self, *, local_connector: str, local_role: str | None = None, service: str | None = None
    ) -> str:
        """Read-only listing, for `/linked roles` - never raises LinkError.
        With a `local_role`, shows just that role's group; without one (or
        with `service == "all"`), lists every group."""
        if local_role is not None and (service is None or service.lower() != "all"):
            local_id = await self._resolve_to_id(local_connector, local_role)
            bridge_group = await self._role_mappings.get_bridge_group(local_connector, local_id)
            if bridge_group is None:
                return "This role isn't linked to any others."
            groups = [await self._role_mappings.get_mapped_roles(bridge_group)]
        else:
            groups_by_id: dict[str, list[RoleMapping]] = {}
            for mapping in await self._role_mappings.get_all():
                groups_by_id.setdefault(mapping.bridge_group, []).append(mapping)
            if not groups_by_id:
                return "No roles are linked yet."
            groups = list(groups_by_id.values())

        lines = []
        for group_mappings in groups:
            parts = await format_linked_listing(
                group_mappings, self._connectors, "role_id", resolve_name=self._resolve_name
            )
            lines.append(" ↔ ".join(parts))
        return "Linked roles:\n" + "\n".join(lines)

    async def unlink_role(self, *, local_connector: str, local_role: str, destination: str | None) -> str:
        """`/unlink role`. `destination` (a connector id) kicks just that one
        member out of the role's bridge group; None/"all" (the default)
        dissolves the whole group. A kick that would strand a lone survivor
        dissolves the group instead (a group of one isn't a bridge)."""
        local_id = await self._resolve_to_id(local_connector, local_role)
        bridge_group = await self._role_mappings.get_bridge_group(local_connector, local_id)
        if bridge_group is None:
            raise LinkError("this role isn't linked to anything.")

        if destination is None or destination.lower() == "all":
            count = await self._role_mappings.delete_bridge_group(bridge_group)
            return f"Unlinked this role's entire bridge group ({count} role(s) removed)."

        mapped = await self._role_mappings.get_mapped_roles(bridge_group)

        async def _dissolve(survivors: list[RoleMapping]) -> None:
            for m in survivors:
                await self._role_mappings.delete_mapping(m.connector_id, m.role_id)

        target, _survivors = await _kick_group_member(
            mapped,
            destination,
            id_attr="role_id",
            not_a_member_message=f"'{destination}' isn't linked in this role's bridge group.",
            delete_mapping=self._role_mappings.delete_mapping,
            dissolve_survivors=_dissolve,
        )
        label = self._connectors[destination].label if destination in self._connectors else destination
        return f"Unlinked {label} role '{target.role_name}' ({target.role_id}) from this bridge group."

    async def _resolve_to_id(self, connector: str, token: str) -> str:
        info = self._connectors.get(connector)
        hook = info.resolve_role_id_by_name if info else None
        return await _resolve_entity_id(token, hook, connector=connector, kind="role")

    async def _resolve_name(self, connector_id: str, role_id: str) -> str:
        info = self._connectors.get(connector_id)
        hook = info.resolve_role_name if info else None
        name = await _resolve_entity_title(role_id, hook, connector=connector_id, kind="role")
        return name or role_id
