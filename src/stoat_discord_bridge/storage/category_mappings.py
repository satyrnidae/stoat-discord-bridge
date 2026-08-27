"""Which Categories, across connectors, are linked to each other - the
Category-level counterpart of channel_mappings.py.

Each mapped Category is stored as its own document keyed by (connector_id,
category_id), tagged with a `bridge_group` tying together every connector's
Category for one linked group. Rows are created only by the `/link-category`
admin command (see admin_commands.py's CategoryLinker) - nothing links
automatically. Once linked, though, a new channel appearing inside either
Category *is* auto-synced (created + linked) onto the other -
CategoryLinker.sync_new_channel, called from each connector's own
channel-create event handler, is what does that.

ThreadCategoryRepository is a separate, unrelated concern that happens to
live in this module: it marks a (connector_id, category_id) as one Discord's
thread/forum-post auto-mirroring created (see
DiscordSenderService._handle_thread_create), so `/link-category` can refuse
to link it - such a Category is dedicated to that per-thread-parent mirroring
flow, and manually linking it would create a second, conflicting sync path
onto the same channels.
"""

from __future__ import annotations

from dataclasses import dataclass

from motor.motor_asyncio import AsyncIOMotorDatabase


@dataclass(frozen=True)
class CategoryMapping:
    bridge_group: str
    connector_id: str
    category_id: str
    category_name: str


class CategoryMappingRepository:
    def __init__(self, db: AsyncIOMotorDatabase) -> None:
        self._collection = db["category_mappings"]

    async def get_bridge_group(self, connector_id: str, category_id: str) -> str | None:
        doc = await self._collection.find_one({"connector_id": connector_id, "category_id": category_id})
        return doc["bridge_group"] if doc else None

    async def get_mapped_categories(self, bridge_group: str) -> list[CategoryMapping]:
        cursor = self._collection.find({"bridge_group": bridge_group})
        return [_from_doc(doc) async for doc in cursor]

    async def upsert(self, mapping: CategoryMapping) -> None:
        await self._collection.update_one(
            {"connector_id": mapping.connector_id, "category_id": mapping.category_id},
            {"$set": {"bridge_group": mapping.bridge_group, "category_name": mapping.category_name}},
            upsert=True,
        )

    async def delete_mapping(self, connector_id: str, category_id: str) -> bool:
        """Removes just this one Category from its bridge group - the rest
        of the group (if any) stays linked to each other. For
        `/unlink-category <destination>`, which kicks a single member rather
        than dissolving the whole group."""
        result = await self._collection.delete_one({"connector_id": connector_id, "category_id": category_id})
        return result.deleted_count > 0

    async def delete_bridge_group(self, bridge_group: str) -> int:
        """Dissolves an entire bridge group - every member Category, not
        just one. For `/unlink-category`'s default ("all") behavior."""
        result = await self._collection.delete_many({"bridge_group": bridge_group})
        return result.deleted_count


def _from_doc(doc: dict) -> CategoryMapping:
    return CategoryMapping(
        bridge_group=doc["bridge_group"],
        connector_id=doc["connector_id"],
        category_id=doc["category_id"],
        category_name=doc["category_name"],
    )


class ThreadCategoryRepository:
    def __init__(self, db: AsyncIOMotorDatabase) -> None:
        self._collection = db["thread_categories"]

    async def mark(self, connector_id: str, category_id: str) -> None:
        await self._collection.update_one(
            {"connector_id": connector_id, "category_id": category_id},
            {"$set": {"connector_id": connector_id, "category_id": category_id}},
            upsert=True,
        )

    async def is_thread_category(self, connector_id: str, category_id: str) -> bool:
        doc = await self._collection.find_one({"connector_id": connector_id, "category_id": category_id})
        return doc is not None
