"""Which roles, across connectors, are linked to each other - the role-level
counterpart of channel_mappings.py / category_mappings.py.

Each mapped role is stored as its own document keyed by (connector_id,
role_id), tagged with a `bridge_group` tying together every connector's role
for one linked group. Rows are created only by the `/link role` / `/mirror
role` admin commands (see admin_commands.py's RoleLinker) - nothing links
automatically.

Roles are Discord/Stoat only - IRC has no role concept, same as Categories.

Once roles are linked, two further behaviors kick in (see bridge.py's
RoleGrantCoordinator and each service's permission-mirror hooks):

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


@dataclass(frozen=True)
class RoleMapping:
    bridge_group: str
    connector_id: str
    role_id: str
    role_name: str


class RoleMappingRepository:
    def __init__(self, db: AsyncIOMotorDatabase) -> None:
        self._collection = db["role_mappings"]

    async def get_bridge_group(self, connector_id: str, role_id: str) -> str | None:
        doc = await self._collection.find_one({"connector_id": connector_id, "role_id": role_id})
        return doc["bridge_group"] if doc else None

    async def get_mapped_roles(self, bridge_group: str) -> list[RoleMapping]:
        cursor = self._collection.find({"bridge_group": bridge_group})
        return [_from_doc(doc) async for doc in cursor]

    async def get_all_for_connector(self, connector_id: str) -> list[RoleMapping]:
        cursor = self._collection.find({"connector_id": connector_id})
        return [_from_doc(doc) async for doc in cursor]

    async def get_all(self) -> list[RoleMapping]:
        """Every linked role, across every connector and group - for the
        `/linked roles` command's "list everything" mode."""
        cursor = self._collection.find({})
        return [_from_doc(doc) async for doc in cursor]

    async def find_linked_role_id(
        self, origin_connector_id: str, origin_role_id: str, target_connector_id: str
    ) -> str | None:
        """If `origin_role_id` (on `origin_connector_id`) is linked to a role
        on `target_connector_id`, return that role's native id there - None if
        unlinked, or linked but with no role recorded for
        `target_connector_id`. Used by the auto-grant and permission-mirror
        flows (see bridge.py's RoleGrantCoordinator). Mirrors
        UserMappingRepository.find_linked_user_id."""
        bridge_group = await self.get_bridge_group(origin_connector_id, origin_role_id)
        if bridge_group is None:
            return None
        for mapping in await self.get_mapped_roles(bridge_group):
            if mapping.connector_id == target_connector_id:
                return mapping.role_id
        return None

    async def upsert(self, mapping: RoleMapping) -> None:
        # A bridge group should have at most one role per connector. Without
        # this, relinking a connector's id within an existing group (e.g.
        # correcting one that was mistyped when first linked) would leave the
        # old, wrong id sitting in the group alongside the new one - same
        # guard as user_mappings.py.
        await self._collection.delete_many(
            {
                "bridge_group": mapping.bridge_group,
                "connector_id": mapping.connector_id,
                "role_id": {"$ne": mapping.role_id},
            }
        )
        await self._collection.update_one(
            {"connector_id": mapping.connector_id, "role_id": mapping.role_id},
            {"$set": {"bridge_group": mapping.bridge_group, "role_name": mapping.role_name}},
            upsert=True,
        )

    async def delete_mapping(self, connector_id: str, role_id: str) -> bool:
        """Removes just this one role from its bridge group - the rest of the
        group (if any) stays linked to each other. For `/unlink role
        <destination>`, which kicks a single member rather than dissolving
        the whole group."""
        result = await self._collection.delete_one({"connector_id": connector_id, "role_id": role_id})
        return result.deleted_count > 0

    async def delete_bridge_group(self, bridge_group: str) -> int:
        """Dissolves an entire bridge group - every member role, not just
        one. For `/unlink role`'s default ("all") behavior."""
        result = await self._collection.delete_many({"bridge_group": bridge_group})
        return result.deleted_count


def _from_doc(doc: dict) -> RoleMapping:
    return RoleMapping(
        bridge_group=doc["bridge_group"],
        connector_id=doc["connector_id"],
        role_id=doc["role_id"],
        role_name=doc["role_name"],
    )
