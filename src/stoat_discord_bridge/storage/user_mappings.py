"""Which user identities, across connectors, refer to the same person -
used to rewrite @mentions into each target connector's native syntax when
relaying a message (see services/mentions.py). Rows are created only by
the `/link-user` admin command (see admin_commands.py) - nothing links
automatically.
"""

from __future__ import annotations

from dataclasses import dataclass

from motor.motor_asyncio import AsyncIOMotorDatabase


@dataclass(frozen=True)
class UserMapping:
    link_group: str
    connector_id: str
    user_id: str
    # For IRC, user_id itself already IS the nick (IRC has no separate id
    # system - same convention channel_mappings.py uses for channel names).
    display_name: str


class UserMappingRepository:
    def __init__(self, db: AsyncIOMotorDatabase) -> None:
        self._collection = db["user_mappings"]

    async def get_link_group(self, connector_id: str, user_id: str) -> str | None:
        doc = await self._collection.find_one({"connector_id": connector_id, "user_id": user_id})
        return doc["link_group"] if doc else None

    async def get_mapped_users(self, link_group: str) -> list[UserMapping]:
        cursor = self._collection.find({"link_group": link_group})
        return [_from_doc(doc) async for doc in cursor]

    async def get_all_for_connector(self, connector_id: str) -> list[UserMapping]:
        cursor = self._collection.find({"connector_id": connector_id})
        return [_from_doc(doc) async for doc in cursor]

    async def upsert(self, mapping: UserMapping) -> None:
        await self._collection.update_one(
            {"connector_id": mapping.connector_id, "user_id": mapping.user_id},
            {"$set": {"link_group": mapping.link_group, "display_name": mapping.display_name}},
            upsert=True,
        )


def _from_doc(doc: dict) -> UserMapping:
    return UserMapping(
        link_group=doc["link_group"],
        connector_id=doc["connector_id"],
        user_id=doc["user_id"],
        display_name=doc["display_name"],
    )
