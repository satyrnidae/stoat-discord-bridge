"""Cross-connector message ID references, keyed by bridge group + origin message.

Lets a future edit/delete-sync feature look up "this Discord message ID
corresponds to these Stoat/IRC message IDs" (and vice versa). Not consumed
anywhere yet — `BridgeCoordinator` just records each relay here.

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
        """Given any one connector's message ID, find every ref (origin + relayed) in its sync group."""
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


def _to_doc(ref: MessageRef) -> dict:
    return {"platform": ref.connector_id, "channel_id": ref.channel_id, "message_id": ref.message_id}


def _from_doc(doc: dict) -> MessageRef:
    return MessageRef(connector_id=doc["platform"], channel_id=doc["channel_id"], message_id=doc["message_id"])
