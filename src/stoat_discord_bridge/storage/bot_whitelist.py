"""Runtime bot allowlist - which bot users, per connector, have their
messages/edits/reactions relayed normally despite being bot-authored (issue
#120). Populated by the `/whitelist add` admin command (see
`admin_commands.bot_whitelist.BotWhitelistManager`), on top of a static
`config.yaml` `whitelisted_bots:` seed that's merged in at check time but
never written here.

Not a `BaseMappingRepository` - a whitelist entry has no cross-connector
bridge group, just a per-(connector, bot user) row, same shape as
`role_mappings.py`'s individual documents but with no `bridge_group` tying
rows together.
"""

from __future__ import annotations

from dataclasses import dataclass

from motor.motor_asyncio import AsyncIOMotorDatabase


@dataclass(frozen=True)
class BotWhitelistEntry:
    connector_id: str
    user_id: str
    label: str | None = None  # the bot's display name at add time, for the listing
    added_by: str | None = None  # the admin user id who ran /whitelist add, for the listing


class BotWhitelistRepository:
    def __init__(self, db: AsyncIOMotorDatabase) -> None:
        self._collection = db["bot_whitelist"]

    async def ensure_indexes(self) -> None:
        """A (connector_id, user_id) pair may appear at most once."""
        await self._collection.create_index([("connector_id", 1), ("user_id", 1)], unique=True)

    async def add(self, entry: BotWhitelistEntry) -> bool:
        """Upsert `entry`. Returns False (no-op) if that (connector_id,
        user_id) was already whitelisted - `$setOnInsert` means an existing
        row's `label`/`added_by` are left as first recorded, not overwritten
        by a repeat `/whitelist add`."""
        result = await self._collection.update_one(
            {"connector_id": entry.connector_id, "user_id": entry.user_id},
            {"$setOnInsert": _to_doc(entry)},
            upsert=True,
        )
        return result.upserted_id is not None

    async def remove(self, connector_id: str, user_id: str) -> bool:
        """Returns False if nothing was removed (wasn't whitelisted)."""
        result = await self._collection.delete_one({"connector_id": connector_id, "user_id": user_id})
        return result.deleted_count > 0

    async def list_for_connector(self, connector_id: str) -> list[BotWhitelistEntry]:
        return [_from_doc(doc) async for doc in self._collection.find({"connector_id": connector_id})]

    async def is_whitelisted(self, connector_id: str, user_id: str) -> bool:
        doc = await self._collection.find_one({"connector_id": connector_id, "user_id": user_id})
        return doc is not None


def _to_doc(entry: BotWhitelistEntry) -> dict:
    return {
        "connector_id": entry.connector_id,
        "user_id": entry.user_id,
        "label": entry.label,
        "added_by": entry.added_by,
    }


def _from_doc(doc: dict) -> BotWhitelistEntry:
    return BotWhitelistEntry(
        connector_id=doc["connector_id"],
        user_id=doc["user_id"],
        label=doc.get("label"),
        added_by=doc.get("added_by"),
    )
