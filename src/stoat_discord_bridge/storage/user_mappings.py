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

    async def find_linked_user_id(self, origin_connector_id: str, origin_user_id: str, target_connector_id: str) -> str | None:
        """If `origin_user_id` (on `origin_connector_id`) is linked to an
        identity on `target_connector_id`, return that identity's native user
        id there - None if unlinked, or linked but with no identity recorded
        for `target_connector_id`. Used by each receiver's receive() to swap
        a relayed message's masquerade to the locally-linked identity."""
        link_group = await self.get_link_group(origin_connector_id, origin_user_id)
        if link_group is None:
            return None
        for mapping in await self.get_mapped_users(link_group):
            if mapping.connector_id == target_connector_id:
                return mapping.user_id
        return None

    async def get_all(self) -> list[UserMapping]:
        """Every linked identity, across every connector and group - for
        the `/linked-users` debugging command's "list everything" mode."""
        cursor = self._collection.find({})
        return [_from_doc(doc) async for doc in cursor]

    async def upsert(self, mapping: UserMapping) -> None:
        # A link group should have at most one identity per connector.
        # Without this, relinking a connector's id within an existing group
        # (e.g. correcting one that was mistyped when first linked) would
        # leave the old, wrong id sitting in the group alongside the new
        # one - and get_mapped_users/mention rewriting would then have two
        # same-connector entries to pick from, nondeterministically.
        await self._collection.delete_many(
            {
                "link_group": mapping.link_group,
                "connector_id": mapping.connector_id,
                "user_id": {"$ne": mapping.user_id},
            }
        )
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
