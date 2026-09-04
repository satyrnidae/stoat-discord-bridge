"""Which channels, across connectors, are bridged to each other.

Each mapped channel is stored as its own document keyed by (connector_id,
channel_id), tagged with a `bridge_group` — a logical name tying together
every connector's channel for one bridged conversation (e.g. "general").

Rows are created by the `/link channel` and `/mirror channel` admin
commands (see admin_commands/channel.py) - nothing links automatically.

The Mongo field is still named "platform" (pre-dating the move to
free-form connector ids) so that a deployment whose config.yaml connector
ids match its old Platform enum values (see config.yaml's seeded ids)
doesn't need a data migration.
"""

from __future__ import annotations

from dataclasses import dataclass

from motor.motor_asyncio import AsyncIOMotorDatabase

from stoat_discord_bridge.storage.base_mapping import BaseMappingRepository


@dataclass(frozen=True)
class ChannelMapping:
    bridge_group: str
    connector_id: str
    channel_id: str
    channel_name: str


class ChannelMappingRepository(BaseMappingRepository[ChannelMapping]):
    def __init__(self, db: AsyncIOMotorDatabase) -> None:
        super().__init__(
            db,
            "channel_mappings",
            _from_doc,
            connector_field="platform",
            id_field="channel_id",
            group_field="bridge_group",
            name_field="channel_name",
        )

    async def get_bridge_group(self, connector_id: str, channel_id: str) -> str | None:
        return await self.get_group(connector_id, channel_id)

    async def get_mapped_channels(self, bridge_group: str) -> list[ChannelMapping]:
        return await self.get_mapped(bridge_group)

    async def delete_bridge_group(self, bridge_group: str) -> int:
        """Dissolves an entire bridge group - every member channel, not just
        one. For `/unlink channel`'s default ("all") behavior."""
        return await self.delete_group(bridge_group)


def _from_doc(doc: dict) -> ChannelMapping:
    return ChannelMapping(
        bridge_group=doc["bridge_group"],
        connector_id=doc["platform"],
        channel_id=doc["channel_id"],
        channel_name=doc["channel_name"],
    )
