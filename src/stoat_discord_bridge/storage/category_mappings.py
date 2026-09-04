"""Which Categories, across connectors, are linked to each other - the
Category-level counterpart of channel_mappings.py.

Each mapped Category is stored as its own document keyed by (connector_id,
category_id), tagged with a `bridge_group` tying together every connector's
Category for one linked group. Rows are created only by the `/link-category`
admin command (see admin_commands/category.py's CategoryLinker) - nothing links
automatically. Once linked, though, a new channel appearing inside either
Category *is* auto-synced (created + linked) onto the other -
CategoryLinker.sync_new_channel, called from each connector's own
channel-create event handler, is what does that.

ThreadCategoryRepository is a separate, unrelated concern that happens to
live in this module: it persistently binds a Discord thread's parent channel
(as its linked counterpart on a Stoat connector) to the Stoat Category that
Discord's thread/forum-post auto-mirroring created for it (see
DiscordSenderService._handle_thread_create). Two jobs:

- `/link-category` refuses to link a Category the repo knows is a thread
  Category (`is_thread_category`) - such a Category is dedicated to the
  per-thread-parent mirroring flow, and manually linking it would create a
  second, conflicting sync path onto the same channels.
- Category placement resolves the thread Category by its stored id, not by
  title, so renaming (or deleting) the Category on Stoat no longer spawns a
  fresh one on the next thread - a missing bound id self-heals into a rebind.

Rows are keyed by (connector_id, parent_channel_id). Legacy rows written by
an earlier version carry only (connector_id, category_id) and no parent link;
they still answer `is_thread_category`, and the next thread for that parent
rewrites them into the current shape.
"""

from __future__ import annotations

from dataclasses import dataclass

from motor.motor_asyncio import AsyncIOMotorDatabase

from stoat_discord_bridge.storage.base_mapping import BaseMappingRepository


@dataclass(frozen=True)
class CategoryMapping:
    bridge_group: str
    connector_id: str
    category_id: str
    category_name: str


class CategoryMappingRepository(BaseMappingRepository[CategoryMapping]):
    def __init__(self, db: AsyncIOMotorDatabase) -> None:
        super().__init__(
            db,
            "category_mappings",
            _from_doc,
            connector_field="connector_id",
            id_field="category_id",
            group_field="bridge_group",
            name_field="category_name",
        )

    async def get_bridge_group(self, connector_id: str, category_id: str) -> str | None:
        return await self.get_group(connector_id, category_id)

    async def get_mapped_categories(self, bridge_group: str) -> list[CategoryMapping]:
        return await self.get_mapped(bridge_group)

    async def delete_bridge_group(self, bridge_group: str) -> int:
        """Dissolves an entire bridge group - every member Category, not
        just one. For `/unlink-category`'s default ("all") behavior."""
        return await self.delete_group(bridge_group)


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

    async def bind(self, connector_id: str, parent_channel_id: str, category_id: str) -> None:
        """Record that `parent_channel_id` (on `connector_id`) is grouped by
        Stoat Category `category_id`. Keyed by (connector_id,
        parent_channel_id), so re-binding the same parent to a new Category
        (after a self-heal) overwrites in place."""
        await self._collection.update_one(
            {"connector_id": connector_id, "parent_channel_id": parent_channel_id},
            {
                "$set": {
                    "connector_id": connector_id,
                    "parent_channel_id": parent_channel_id,
                    "category_id": category_id,
                }
            },
            upsert=True,
        )

    async def get_category_id(self, connector_id: str, parent_channel_id: str) -> str | None:
        doc = await self._collection.find_one(
            {"connector_id": connector_id, "parent_channel_id": parent_channel_id}
        )
        return doc["category_id"] if doc else None

    async def get_parent_channel_id(self, connector_id: str, category_id: str) -> str | None:
        doc = await self._collection.find_one({"connector_id": connector_id, "category_id": category_id})
        return doc.get("parent_channel_id") if doc else None

    async def forget(self, connector_id: str, parent_channel_id: str) -> None:
        """Drop the binding for a parent whose bound Category no longer exists
        on the server - the next thread event rebinds it to a fresh one."""
        await self._collection.delete_one(
            {"connector_id": connector_id, "parent_channel_id": parent_channel_id}
        )

    async def is_thread_category(self, connector_id: str, category_id: str) -> bool:
        doc = await self._collection.find_one({"connector_id": connector_id, "category_id": category_id})
        return doc is not None
