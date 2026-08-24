"""Cross-platform custom emoji ID references, one group per mirrored emoji.

When a custom emoji is created on one platform, `BridgeCoordinator.handle_emoji_created`
mirrors it onto every other platform it can and records the native emoji ID
it got on each here, keyed together as one group. A later reaction carrying
that emoji looks up its equivalent ID on the reaction's target platform via
`find_equivalent` — a miss means "never mirrored there" (creation failed, or
hasn't happened yet), which callers should treat as "skip this reaction",
not an error.
"""

from __future__ import annotations

from dataclasses import dataclass

from bson import ObjectId
from motor.motor_asyncio import AsyncIOMotorDatabase
from pymongo.errors import DuplicateKeyError

from stoat_discord_bridge.models import Platform


@dataclass(frozen=True)
class EmojiRef:
    platform: Platform
    emoji_id: str
    name: str


class EmojiMappingRepository:
    def __init__(self, db: AsyncIOMotorDatabase) -> None:
        self._collection = db["emoji_mappings"]

    async def ensure_indexes(self) -> None:
        """A (platform, emoji_id) pair may appear in at most one mapping
        group. Enforced in Mongo, not just checked-then-acted-on in Python,
        so `try_reserve` below can rely on a duplicate-key error to detect a
        concurrent/duplicate creation event atomically."""
        await self._collection.create_index([("refs.platform", 1), ("refs.emoji_id", 1)], unique=True)

    async def try_reserve(self, origin: EmojiRef) -> str | None:
        """Atomically claim `origin` as the start of a new mapping group,
        returning the new group's id — or None if it's already part of some
        group (a duplicate or self-echoed creation event for an
        already-known emoji). This insert is the only place a group is
        created, and the unique index makes it race-proof: two concurrent
        calls for the same (platform, emoji_id) can't both succeed, unlike
        a separate exists()-check-then-record() pair would allow."""
        try:
            result = await self._collection.insert_one({"refs": [_to_doc(origin)]})
        except DuplicateKeyError:
            return None
        return str(result.inserted_id)

    async def add_refs(self, group_id: str, refs: list[EmojiRef]) -> None:
        """Record newly-mirrored copies against a group from try_reserve."""
        if not refs:
            return
        await self._collection.update_one(
            {"_id": ObjectId(group_id)}, {"$push": {"refs": {"$each": [_to_doc(ref) for ref in refs]}}}
        )

    async def release(self, group_id: str) -> None:
        """Drop a reservation that ended up mirroring onto no other
        platform, so a later retry isn't permanently blocked by the unique
        index from ever reserving this emoji again."""
        await self._collection.delete_one({"_id": ObjectId(group_id)})

    async def find_equivalent(self, platform: Platform, emoji_id: str, target_platform: Platform) -> str | None:
        doc = await self._collection.find_one(
            {"refs": {"$elemMatch": {"platform": platform.value, "emoji_id": emoji_id}}}
        )
        if doc is None:
            return None
        for ref in doc["refs"]:
            if ref["platform"] == target_platform.value:
                return ref["emoji_id"]
        return None

    async def forget(self, platform: Platform, emoji_id: str) -> None:
        """Drop `platform`'s ref from the group containing (platform, emoji_id) —
        called when that platform's copy is deleted. The group itself is only
        deleted once every platform's copy is gone; a ref still remaining
        elsewhere means a reaction using it there should keep resolving."""
        doc = await self._collection.find_one(
            {"refs": {"$elemMatch": {"platform": platform.value, "emoji_id": emoji_id}}}
        )
        if doc is None:
            return
        remaining = [
            ref for ref in doc["refs"] if not (ref["platform"] == platform.value and ref["emoji_id"] == emoji_id)
        ]
        if remaining:
            await self._collection.update_one({"_id": doc["_id"]}, {"$set": {"refs": remaining}})
        else:
            await self._collection.delete_one({"_id": doc["_id"]})


def _to_doc(ref: EmojiRef) -> dict:
    return {"platform": ref.platform.value, "emoji_id": ref.emoji_id, "name": ref.name}
