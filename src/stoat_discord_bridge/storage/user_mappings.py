"""Which user identities, across connectors, refer to the same person - used
to rewrite @mentions into each target connector's native syntax when relaying
a message (see services/mentions.py), and to make a linked sender's relayed
masquerade show their local identity instead of their remote one (see each
receiver's receive(), via find_linked_user_id below). Rows are created only
by the `/link-user` admin command (see admin_commands.py) - nothing links
automatically.
"""

from __future__ import annotations

from dataclasses import dataclass

from motor.motor_asyncio import AsyncIOMotorDatabase

from stoat_discord_bridge.storage.base_mapping import BaseMappingRepository


@dataclass(frozen=True)
class UserMapping:
    link_group: str
    connector_id: str
    user_id: str
    # For IRC, user_id itself already IS the nick (IRC has no separate id
    # system - same convention channel_mappings.py uses for channel names).
    display_name: str


class UserMappingRepository(BaseMappingRepository[UserMapping]):
    def __init__(self, db: AsyncIOMotorDatabase) -> None:
        super().__init__(
            db,
            "user_mappings",
            _from_doc,
            connector_field="connector_id",
            id_field="user_id",
            group_field="link_group",
            name_field="display_name",
            dedup_on_upsert=True,
        )

    async def get_link_group(self, connector_id: str, user_id: str) -> str | None:
        return await self.get_group(connector_id, user_id)

    async def get_mapped_users(self, link_group: str) -> list[UserMapping]:
        return await self.get_mapped(link_group)

    async def find_linked_user_id(self, origin_connector_id: str, origin_user_id: str, target_connector_id: str) -> str | None:
        """If `origin_user_id` (on `origin_connector_id`) is linked to an
        identity on `target_connector_id`, return that identity's native user
        id there - None if unlinked, or linked but with no identity recorded
        for `target_connector_id`. Used by each receiver's receive() to swap
        a relayed message's masquerade to the locally-linked identity."""
        return await self.find_linked_id(origin_connector_id, origin_user_id, target_connector_id)

    async def delete_link_group(self, link_group: str) -> int:
        """Dissolves an entire link group - every linked identity, not just
        one. For `/unlink-user`'s default ("all") behavior."""
        return await self.delete_group(link_group)


def _from_doc(doc: dict) -> UserMapping:
    return UserMapping(
        link_group=doc["link_group"],
        connector_id=doc["connector_id"],
        user_id=doc["user_id"],
        display_name=doc["display_name"],
    )
