"""Which roles, across connectors, are linked to each other - the role-level
counterpart of channel_mappings.py / category_mappings.py.

Each mapped role is stored as its own document keyed by (connector_id,
role_id), tagged with a `bridge_group` tying together every connector's role
for one linked group. Rows are created only by the `/link role` / `/mirror
role` admin commands (see admin_commands/role.py's RoleLinker) - nothing links
automatically.

Roles are Discord/Stoat only - IRC has no role concept, same as Categories.

Once roles are linked, two further behaviors kick in (see bridge.py's
RoleSyncCoordinator and each service's permission-mirror hooks):

- a linked user gaining/losing a linked role on one connector has the linked
  role granted/revoked for their linked identity on the other, and
- a linked role's per-channel permission override changing on one connector
  is mirrored onto the linked channel's copy for the linked role on the other
  (only for channels/categories that are themselves bridge-linked).

Unlike channel_mappings.py this repo uses `connector_id` as the real Mongo
field name (channel_mappings.py carries a legacy `platform` field for
backward compat with older deployments; this collection is new, so there's
nothing to stay compatible with).
"""

from __future__ import annotations

from dataclasses import dataclass

from motor.motor_asyncio import AsyncIOMotorDatabase

from stoat_discord_bridge.storage.base_mapping import BaseMappingRepository


@dataclass(frozen=True)
class RoleMapping:
    bridge_group: str
    connector_id: str
    role_id: str
    role_name: str


class RoleMappingRepository(BaseMappingRepository[RoleMapping]):
    def __init__(self, db: AsyncIOMotorDatabase) -> None:
        super().__init__(
            db,
            "role_mappings",
            _from_doc,
            connector_field="connector_id",
            id_field="role_id",
            group_field="bridge_group",
            name_field="role_name",
            dedup_on_upsert=True,
        )

    async def get_bridge_group(self, connector_id: str, role_id: str) -> str | None:
        return await self.get_group(connector_id, role_id)

    async def get_mapped_roles(self, bridge_group: str) -> list[RoleMapping]:
        return await self.get_mapped(bridge_group)

    async def find_linked_role_id(
        self, origin_connector_id: str, origin_role_id: str, target_connector_id: str
    ) -> str | None:
        """If `origin_role_id` (on `origin_connector_id`) is linked to a role
        on `target_connector_id`, return that role's native id there - None if
        unlinked, or linked but with no role recorded for
        `target_connector_id`. Used by the auto-grant and permission-mirror
        flows (see bridge.py's RoleSyncCoordinator). Mirrors
        UserMappingRepository.find_linked_user_id."""
        return await self.find_linked_id(origin_connector_id, origin_role_id, target_connector_id)

    async def delete_bridge_group(self, bridge_group: str) -> int:
        """Dissolves an entire bridge group - every member role, not just
        one. For `/unlink role`'s default ("all") behavior."""
        return await self.delete_group(bridge_group)


def _from_doc(doc: dict) -> RoleMapping:
    return RoleMapping(
        bridge_group=doc["bridge_group"],
        connector_id=doc["connector_id"],
        role_id=doc["role_id"],
        role_name=doc["role_name"],
    )
