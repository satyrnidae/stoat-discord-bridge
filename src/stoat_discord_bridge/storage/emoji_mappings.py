"""Cross-connector custom emoji ID references, one group per mirrored emoji.

When a custom emoji is created on one connector, `BridgeCoordinator.handle_emoji_created`
mirrors it onto every other connector it can and records the native emoji ID
it got on each here, keyed together as one group. A later reaction carrying
that emoji looks up its equivalent ID on the reaction's target connector via
`find_equivalent` — a miss means "never mirrored there" (creation failed, or
hasn't happened yet), which callers should treat as "skip this reaction",
not an error.

The Mongo field is still named "platform" (pre-dating the move to free-form
connector ids) for the same backward-compatibility reason noted in
channel_mappings.py.
"""

from __future__ import annotations

from dataclasses import dataclass

from bson import ObjectId
from motor.motor_asyncio import AsyncIOMotorDatabase
from pymongo.errors import DuplicateKeyError


@dataclass(frozen=True)
class EmojiRef:
    connector_id: str
    emoji_id: str
    name: str


class EmojiMappingRepository:
    def __init__(self, db: AsyncIOMotorDatabase) -> None:
        self._collection = db["emoji_mappings"]

    async def ensure_indexes(self) -> None:
        """A (connector_id, emoji_id) pair may appear in at most one mapping
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
        calls for the same (connector_id, emoji_id) can't both succeed, unlike
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
        connector, so a later retry isn't permanently blocked by the unique
        index from ever reserving this emoji again."""
        await self._collection.delete_one({"_id": ObjectId(group_id)})

    async def get_group_id(self, connector_id: str, emoji_id: str) -> str | None:
        """Which mapping group (if any) already contains this (connector_id,
        emoji_id) ref - used by EmoteLinker to detect/merge with an existing
        group when manually linking two already-existing emoji."""
        doc = await self._collection.find_one(
            {"refs": {"$elemMatch": {"platform": connector_id, "emoji_id": emoji_id}}}
        )
        return str(doc["_id"]) if doc else None

    async def get_refs(self, group_id: str) -> list[EmojiRef]:
        """Every ref in `group_id` - for `/linked emotes` and
        `EmoteLinker.unlink_emote`'s survivor bookkeeping."""
        doc = await self._collection.find_one({"_id": ObjectId(group_id)})
        if doc is None:
            return []
        return [EmojiRef(connector_id=r["platform"], emoji_id=r["emoji_id"], name=r["name"]) for r in doc["refs"]]

    async def get_all_groups(self) -> dict[str, list[EmojiRef]]:
        """Every mapping group, keyed by group id - for the no-argument
        `/linked emotes` listing."""
        groups: dict[str, list[EmojiRef]] = {}
        async for doc in self._collection.find({}):
            groups[str(doc["_id"])] = [
                EmojiRef(connector_id=r["platform"], emoji_id=r["emoji_id"], name=r["name"]) for r in doc["refs"]
            ]
        return groups

    async def delete_ref(self, connector_id: str, emoji_id: str) -> None:
        """Pull just `connector_id`'s ref from whatever group holds it, with
        no group cleanup - `EmoteLinker.unlink_emote` decides when a group is
        no longer a bridge (unlike `forget`, which is delete-sync bookkeeping
        and keeps the group alive until every copy is gone)."""
        doc = await self._collection.find_one(
            {"refs": {"$elemMatch": {"platform": connector_id, "emoji_id": emoji_id}}}
        )
        if doc is None:
            return
        remaining = [
            ref for ref in doc["refs"] if not (ref["platform"] == connector_id and ref["emoji_id"] == emoji_id)
        ]
        await self._collection.update_one({"_id": doc["_id"]}, {"$set": {"refs": remaining}})

    async def delete_group(self, group_id: str) -> int:
        """Drop a whole mapping group - `/unlink emote` with no/`all` target.
        Returns the number of refs it held."""
        doc = await self._collection.find_one({"_id": ObjectId(group_id)})
        if doc is None:
            return 0
        await self._collection.delete_one({"_id": doc["_id"]})
        return len(doc["refs"])

    async def find_equivalent(self, connector_id: str, emoji_id: str, target_connector_id: str) -> str | None:
        ref = await self.find_equivalent_ref(connector_id, emoji_id, target_connector_id)
        return ref.emoji_id if ref is not None else None

    async def find_equivalent_ref(
        self, connector_id: str, emoji_id: str, target_connector_id: str
    ) -> EmojiRef | None:
        """Like `find_equivalent` but returns the whole target ref (id + name)
        - message-content emoji rewriting needs the name too."""
        doc = await self._collection.find_one(
            {"refs": {"$elemMatch": {"platform": connector_id, "emoji_id": emoji_id}}}
        )
        if doc is None:
            return None
        for ref in doc["refs"]:
            if ref["platform"] == target_connector_id:
                return EmojiRef(connector_id=ref["platform"], emoji_id=ref["emoji_id"], name=ref["name"])
        return None

    async def find_name(self, connector_id: str, emoji_id: str) -> str | None:
        """The stored name of a single (connector_id, emoji_id) ref, from
        whatever group holds it - IRC emote-stripping needs the name even
        though IRC never has its own linked copy."""
        doc = await self._collection.find_one(
            {"refs": {"$elemMatch": {"platform": connector_id, "emoji_id": emoji_id}}}
        )
        if doc is None:
            return None
        for ref in doc["refs"]:
            if ref["platform"] == connector_id and ref["emoji_id"] == emoji_id:
                return ref["name"]
        return None

    async def forget(self, connector_id: str, emoji_id: str) -> None:
        """Drop `connector_id`'s ref from the group containing (connector_id, emoji_id) —
        called when that connector's copy is deleted. The group itself is only
        deleted once every connector's copy is gone; a ref still remaining
        elsewhere means a reaction using it there should keep resolving."""
        doc = await self._collection.find_one(
            {"refs": {"$elemMatch": {"platform": connector_id, "emoji_id": emoji_id}}}
        )
        if doc is None:
            return
        remaining = [
            ref for ref in doc["refs"] if not (ref["platform"] == connector_id and ref["emoji_id"] == emoji_id)
        ]
        if remaining:
            await self._collection.update_one({"_id": doc["_id"]}, {"$set": {"refs": remaining}})
        else:
            await self._collection.delete_one({"_id": doc["_id"]})


def _to_doc(ref: EmojiRef) -> dict:
    return {"platform": ref.connector_id, "emoji_id": ref.emoji_id, "name": ref.name}
