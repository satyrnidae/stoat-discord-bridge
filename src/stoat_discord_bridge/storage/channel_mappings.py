"""Which channels, across connectors, are bridged to each other.

Each mapped channel is stored as its own document keyed by (connector_id,
channel_id), tagged with a `bridge_group` — a logical name tying together
every connector's channel for one bridged conversation (e.g. "general").

Rows are created by the `/link channel` and `/mirror-channels` admin
commands (see admin_commands.py) - nothing links automatically.

The Mongo field is still named "platform" (pre-dating the move to
free-form connector ids) so that a deployment whose config.yaml connector
ids match its old Platform enum values (see config.yaml's seeded ids)
doesn't need a data migration.
"""

from __future__ import annotations

from dataclasses import dataclass

from motor.motor_asyncio import AsyncIOMotorDatabase


@dataclass(frozen=True)
class ChannelMapping:
    bridge_group: str
    connector_id: str
    channel_id: str
    channel_name: str


class ChannelMappingRepository:
    def __init__(self, db: AsyncIOMotorDatabase) -> None:
        self._collection = db["channel_mappings"]

    async def get_bridge_group(self, connector_id: str, channel_id: str) -> str | None:
        doc = await self._collection.find_one({"platform": connector_id, "channel_id": channel_id})
        return doc["bridge_group"] if doc else None

    async def get_mapped_channels(self, bridge_group: str) -> list[ChannelMapping]:
        cursor = self._collection.find({"bridge_group": bridge_group})
        return [_from_doc(doc) async for doc in cursor]

    async def get_all_for_connector(self, connector_id: str) -> list[ChannelMapping]:
        cursor = self._collection.find({"platform": connector_id})
        return [_from_doc(doc) async for doc in cursor]

    async def upsert(self, mapping: ChannelMapping) -> None:
        await self._collection.update_one(
            {"platform": mapping.connector_id, "channel_id": mapping.channel_id},
            {"$set": {"bridge_group": mapping.bridge_group, "channel_name": mapping.channel_name}},
            upsert=True,
        )

    async def delete_mapping(self, connector_id: str, channel_id: str) -> bool:
        """Removes just this one channel from its bridge group - the rest of
        the group (if any) stays linked to each other. For `/unlink channel
        <destination>`, which kicks a single member rather than dissolving
        the whole group."""
        result = await self._collection.delete_one({"platform": connector_id, "channel_id": channel_id})
        return result.deleted_count > 0

    async def delete_bridge_group(self, bridge_group: str) -> int:
        """Dissolves an entire bridge group - every member channel, not just
        one. For `/unlink channel`'s default ("all") behavior."""
        result = await self._collection.delete_many({"bridge_group": bridge_group})
        return result.deleted_count


def _from_doc(doc: dict) -> ChannelMapping:
    return ChannelMapping(
        bridge_group=doc["bridge_group"],
        connector_id=doc["platform"],
        channel_id=doc["channel_id"],
        channel_name=doc["channel_name"],
    )
