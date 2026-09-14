"""Cross-connector message ID references, keyed by bridge group + origin message.

Lets reaction / pin / edit / delete sync look up "this Discord message ID
corresponds to these Stoat/IRC message IDs" (and vice versa) via
`find_group`. `BridgeCoordinator` records each relay here as it happens.
`find_group_if_origin` is the same lookup restricted to the *origin* side
only - pin sync is one-way (a pin/unpin on a relayed copy stays local to that
platform) and delete sync must never cascade from a relayed copy back to the
source or other mirrors (a moderator can delete any message, including the
bridge's own posts), so `handle_pin` and `handle_delete` use it instead of
`find_group`; reaction and edit sync stay on `find_group`.

The Mongo field is still named "platform" (pre-dating the move to free-form
connector ids) for the same backward-compatibility reason noted in
channel_mappings.py.
"""

from __future__ import annotations

from dataclasses import dataclass

from motor.motor_asyncio import AsyncIOMotorDatabase


@dataclass(frozen=True)
class MessageRef:
    connector_id: str
    channel_id: str
    message_id: str


class MessageSyncRepository:
    def __init__(self, db: AsyncIOMotorDatabase) -> None:
        self._collection = db["message_sync"]

    async def record(self, bridge_group: str, origin: MessageRef, relayed: list[MessageRef]) -> None:
        await self._collection.insert_one(
            {
                "bridge_group": bridge_group,
                "origin": _to_doc(origin),
                "relayed": [_to_doc(ref) for ref in relayed],
            }
        )

    async def find_group(self, connector_id: str, channel_id: str, message_id: str) -> list[MessageRef] | None:
        """Given any one connector's message ID, find every ref (origin + relayed) in its sync group.

        The returned list always puts the origin ref first, followed by the
        relayed refs in the order `record` was given them."""
        doc = await self._collection.find_one(
            {
                "$or": [
                    {
                        "origin.platform": connector_id,
                        "origin.channel_id": channel_id,
                        "origin.message_id": message_id,
                    },
                    {
                        "relayed": {
                            "$elemMatch": {
                                "platform": connector_id,
                                "channel_id": channel_id,
                                "message_id": message_id,
                            }
                        }
                    },
                ]
            }
        )
        if doc is None:
            return None
        return [_from_doc(doc["origin"]), *(_from_doc(ref) for ref in doc["relayed"])]

    async def find_group_if_origin(
        self, connector_id: str, channel_id: str, message_id: str
    ) -> list[MessageRef] | None:
        """Like `find_group`, but only matches when the given message ID is
        the sync group's recorded *origin* - never a relayed copy. Returns
        `None` both when the message isn't tracked at all and when it's
        tracked only as a relayed copy; callers that only need "is there
        anything to propagate from here" don't need to distinguish those two
        cases."""
        doc = await self._collection.find_one(
            {
                "origin.platform": connector_id,
                "origin.channel_id": channel_id,
                "origin.message_id": message_id,
            }
        )
        if doc is None:
            return None
        return [_from_doc(doc["origin"]), *(_from_doc(ref) for ref in doc["relayed"])]


def _to_doc(ref: MessageRef) -> dict:
    return {"platform": ref.connector_id, "channel_id": ref.channel_id, "message_id": ref.message_id}


def _from_doc(doc: dict) -> MessageRef:
    return MessageRef(connector_id=doc["platform"], channel_id=doc["channel_id"], message_id=doc["message_id"])
