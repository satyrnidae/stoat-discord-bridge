"""Shared CRUD shape behind `ChannelMappingRepository`, `CategoryMappingRepository`,
`RoleMappingRepository`, and `UserMappingRepository` (issue #106) - each links
native entities across connectors into a group (a "bridge group" or "link
group") and stores every connector's member as its own document keyed by
(connector field, id field): `get_group`/`get_mapped`/`get_all_for_connector`/
`get_all`/`find_linked_id`/`upsert`/`delete_mapping`/`delete_group`. Field
*names* differ per collection - `channel_mappings.py`'s legacy `"platform"`
field, `"bridge_group"` vs `"link_group"`, ... - so this is parameterized by
them rather than assuming a fixed schema; each concrete repository keeps its
own original public method names (`get_bridge_group`, `delete_link_group`,
...) as thin wrappers around the generic ones below, so nothing calling them
needs to change.

`EmojiMappingRepository`'s array-of-refs-in-one-document model is different
(the reservation-based group semantics documented on that module) and isn't
built on this.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Generic, TypeVar

from motor.motor_asyncio import AsyncIOMotorDatabase

T = TypeVar("T")


class BaseMappingRepository(Generic[T]):
    def __init__(
        self,
        db: AsyncIOMotorDatabase,
        collection_name: str,
        from_doc: Callable[[dict], T],
        *,
        connector_field: str,
        id_field: str,
        group_field: str,
        name_field: str,
        dedup_on_upsert: bool = False,
    ) -> None:
        """`connector_field`/`id_field`/`group_field`/`name_field` are the
        Mongo document field names for a member's connector id, native
        entity id, group id, and display name respectively. `id_field`,
        `group_field` and `name_field` double as the matching attribute
        names on `T` - every mapping dataclass in this package happens to use
        the same name for both; only the connector field itself sometimes
        doesn't (`channel_mappings.py`'s legacy `"platform"`), so `T`'s
        connector id is always read via `mapping.connector_id` directly
        rather than through a configurable attribute name. `dedup_on_upsert`,
        when True, drops any other document already claiming this connector
        within the same group before writing - a group should have at most
        one member per connector (Role/User use this; Channel/Category
        don't)."""
        self._collection = db[collection_name]
        self._from_doc = from_doc
        self._connector_field = connector_field
        self._id_field = id_field
        self._group_field = group_field
        self._name_field = name_field
        self._dedup_on_upsert = dedup_on_upsert

    async def get_group(self, connector_id: str, entity_id: str) -> str | None:
        doc = await self._collection.find_one({self._connector_field: connector_id, self._id_field: entity_id})
        return doc[self._group_field] if doc else None

    async def get_mapped(self, group: str) -> list[T]:
        cursor = self._collection.find({self._group_field: group})
        return [self._from_doc(doc) async for doc in cursor]

    async def get_all_for_connector(self, connector_id: str) -> list[T]:
        cursor = self._collection.find({self._connector_field: connector_id})
        return [self._from_doc(doc) async for doc in cursor]

    async def get_all(self) -> list[T]:
        """Every mapping, across every connector and group - for a "list
        everything" listing mode."""
        cursor = self._collection.find({})
        return [self._from_doc(doc) async for doc in cursor]

    async def find_linked_id(
        self, origin_connector_id: str, origin_entity_id: str, target_connector_id: str
    ) -> str | None:
        """If `origin_entity_id` (on `origin_connector_id`) is linked to an
        entity on `target_connector_id`, return that entity's native id
        there - None if unlinked, or linked but with no entity recorded for
        `target_connector_id`."""
        group = await self.get_group(origin_connector_id, origin_entity_id)
        if group is None:
            return None
        for mapping in await self.get_mapped(group):
            if _connector_id(mapping) == target_connector_id:
                return getattr(mapping, self._id_field)
        return None

    async def upsert(self, mapping: T) -> None:
        connector_id = _connector_id(mapping)
        entity_id = getattr(mapping, self._id_field)
        group = getattr(mapping, self._group_field)
        name = getattr(mapping, self._name_field)
        if self._dedup_on_upsert:
            # A group should have at most one member per connector. Without
            # this, relinking a connector's id within an existing group (e.g.
            # correcting one that was mistyped when first linked) would leave
            # the old, wrong id sitting in the group alongside the new one.
            await self._collection.delete_many(
                {self._group_field: group, self._connector_field: connector_id, self._id_field: {"$ne": entity_id}}
            )
        await self._collection.update_one(
            {self._connector_field: connector_id, self._id_field: entity_id},
            {"$set": {self._group_field: group, self._name_field: name}},
            upsert=True,
        )

    async def delete_mapping(self, connector_id: str, entity_id: str) -> bool:
        """Removes just this one member from its group - the rest of the
        group (if any) stays linked to each other. For `/unlink <x>
        <destination>`, which kicks a single member rather than dissolving
        the whole group."""
        result = await self._collection.delete_one({self._connector_field: connector_id, self._id_field: entity_id})
        return result.deleted_count > 0

    async def delete_group(self, group: str) -> int:
        """Dissolves an entire group - every member, not just one. For
        `/unlink <x>`'s default ("all") behavior."""
        result = await self._collection.delete_many({self._group_field: group})
        return result.deleted_count


def _connector_id(mapping: Any) -> str:
    return mapping.connector_id
