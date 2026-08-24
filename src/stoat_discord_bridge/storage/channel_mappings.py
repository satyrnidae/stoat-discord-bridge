"""Which channels, across platforms, are bridged to each other.

Each mapped channel is stored as its own document keyed by (platform,
channel_id), tagged with a `bridge_group` — a logical name tying together
every platform's channel for one bridged conversation (e.g. "general").

TODO: this is seeded/edited by hand for now; automatic channel-creation sync
across platforms (mirroring a new channel on one platform onto the others)
is a later goal.
"""

from __future__ import annotations

from dataclasses import dataclass

from motor.motor_asyncio import AsyncIOMotorDatabase

from stoat_discord_bridge.models import Platform


@dataclass(frozen=True)
class ChannelMapping:
    bridge_group: str
    platform: Platform
    channel_id: str
    channel_name: str


class ChannelMappingRepository:
    def __init__(self, db: AsyncIOMotorDatabase) -> None:
        self._collection = db["channel_mappings"]

    async def get_bridge_group(self, platform: Platform, channel_id: str) -> str | None:
        doc = await self._collection.find_one({"platform": platform.value, "channel_id": channel_id})
        return doc["bridge_group"] if doc else None

    async def get_mapped_channels(self, bridge_group: str) -> list[ChannelMapping]:
        cursor = self._collection.find({"bridge_group": bridge_group})
        return [_from_doc(doc) async for doc in cursor]

    async def get_all_for_platform(self, platform: Platform) -> list[ChannelMapping]:
        cursor = self._collection.find({"platform": platform.value})
        return [_from_doc(doc) async for doc in cursor]

    async def upsert(self, mapping: ChannelMapping) -> None:
        await self._collection.update_one(
            {"platform": mapping.platform.value, "channel_id": mapping.channel_id},
            {"$set": {"bridge_group": mapping.bridge_group, "channel_name": mapping.channel_name}},
            upsert=True,
        )


def _from_doc(doc: dict) -> ChannelMapping:
    return ChannelMapping(
        bridge_group=doc["bridge_group"],
        platform=Platform(doc["platform"]),
        channel_id=doc["channel_id"],
        channel_name=doc["channel_name"],
    )
