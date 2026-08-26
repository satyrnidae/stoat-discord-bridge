"""MongoDB connection for sync-tracking data (channel mappings, cross-platform
message ID references)."""

from __future__ import annotations

import logging

from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase

from stoat_discord_bridge.config import MongoConfig

logger = logging.getLogger(__name__)


class MongoStore:
    def __init__(self, config: MongoConfig) -> None:
        self._client: AsyncIOMotorClient = AsyncIOMotorClient(config.uri)
        self.db: AsyncIOMotorDatabase = self._client[config.db_name]
        logger.info("connecting to MongoDB database %r", config.db_name)

    def close(self) -> None:
        logger.info("closing MongoDB connection")
        self._client.close()
