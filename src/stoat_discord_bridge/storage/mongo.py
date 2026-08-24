"""MongoDB connection for sync-tracking data (channel mappings, cross-platform
message ID references)."""

from __future__ import annotations

from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase

from stoat_discord_bridge.config import MongoConfig


class MongoStore:
    def __init__(self, config: MongoConfig) -> None:
        self._client: AsyncIOMotorClient = AsyncIOMotorClient(config.uri)
        self.db: AsyncIOMotorDatabase = self._client[config.db_name]

    def close(self) -> None:
        self._client.close()
