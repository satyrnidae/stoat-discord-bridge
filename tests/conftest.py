"""Shared test fixtures.

fake_mongo provides a minimal in-memory stand-in for the subset of Motor's
async collection API this codebase's storage/*.py repositories actually
use (find_one/find/insert_one/update_one/delete_one/delete_many, plus the
$or/$elemMatch/$ne/$set/$push operators and dotted-path field queries the
message sync/emoji/channel/user mapping repos rely on) - just enough to
exercise their real query/update logic without a live MongoDB.
"""

from __future__ import annotations

import pytest
from bson import ObjectId


class FakeCursor:
    def __init__(self, docs):
        self._docs = list(docs)

    def __aiter__(self):
        self._iter = iter(self._docs)
        return self

    async def __anext__(self):
        try:
            return next(self._iter)
        except StopIteration:
            raise StopAsyncIteration


class FakeCollection:
    def __init__(self):
        self.docs: dict[str, dict] = {}
        self._unique_refs_index = False

    async def create_index(self, keys, unique: bool = False, **kwargs) -> None:
        # Only the one compound unique index this codebase actually creates
        # (EmojiMappingRepository.ensure_indexes, on refs.platform/refs.emoji_id)
        # is understood here - enough to make try_reserve's DuplicateKeyError
        # path testable without a real MongoDB.
        if unique and {k for k, _ in keys} == {"refs.platform", "refs.emoji_id"}:
            self._unique_refs_index = True

    async def insert_one(self, doc: dict):
        if self._unique_refs_index and "refs" in doc:
            for new_ref in doc["refs"]:
                for existing in self.docs.values():
                    if any(
                        r["platform"] == new_ref["platform"] and r["emoji_id"] == new_ref["emoji_id"]
                        for r in existing.get("refs", [])
                    ):
                        from pymongo.errors import DuplicateKeyError

                        raise DuplicateKeyError("duplicate ref")
        _id = ObjectId()
        self.docs[str(_id)] = {**doc, "_id": _id}
        return type("InsertResult", (), {"inserted_id": _id})()

    async def find_one(self, query: dict) -> dict | None:
        for doc in self.docs.values():
            if _matches(doc, query):
                return doc
        return None

    def find(self, query: dict) -> FakeCursor:
        return FakeCursor(doc for doc in self.docs.values() if _matches(doc, query))

    async def update_one(self, query: dict, update: dict, upsert: bool = False) -> None:
        for doc in self.docs.values():
            if _matches(doc, query):
                _apply_update(doc, update)
                return
        if upsert:
            new_doc = {k: v for k, v in query.items() if not isinstance(v, dict)}
            _apply_update(new_doc, update)
            new_doc["_id"] = ObjectId()
            self.docs[str(new_doc["_id"])] = new_doc

    async def delete_one(self, query: dict):
        for key, doc in list(self.docs.items()):
            if _matches(doc, query):
                del self.docs[key]
                return type("DeleteResult", (), {"deleted_count": 1})()
        return type("DeleteResult", (), {"deleted_count": 0})()

    async def delete_many(self, query: dict):
        count = 0
        for key, doc in list(self.docs.items()):
            if _matches(doc, query):
                del self.docs[key]
                count += 1
        return type("DeleteResult", (), {"deleted_count": count})()


def _matches(doc: dict, query: dict) -> bool:
    if "$or" in query:
        return any(_matches(doc, sub_query) for sub_query in query["$or"])
    for key, value in query.items():
        if key == "_id":
            if str(doc.get("_id")) != str(value):
                return False
            continue
        if isinstance(value, dict) and "$elemMatch" in value:
            cond = value["$elemMatch"]
            if not any(all(item.get(ck) == cv for ck, cv in cond.items()) for item in _dotted_get(doc, key) or []):
                return False
            continue
        if isinstance(value, dict) and "$ne" in value:
            if _dotted_get(doc, key) == value["$ne"]:
                return False
            continue
        if _dotted_get(doc, key) != value:
            return False
    return True


def _dotted_get(doc: dict, path: str):
    """MessageSyncRepository.find_group queries nested fields via dotted
    paths (e.g. "origin.platform"), same as real MongoDB - walk the path
    segment by segment instead of a flat dict lookup."""
    value: object = doc
    for part in path.split("."):
        if not isinstance(value, dict):
            return None
        value = value.get(part)
    return value


def _apply_update(doc: dict, update: dict) -> None:
    if "$set" in update:
        doc.update(update["$set"])
    if "$push" in update:
        for key, value in update["$push"].items():
            doc.setdefault(key, [])
            if isinstance(value, dict) and "$each" in value:
                doc[key].extend(value["$each"])
            else:
                doc[key].append(value)


class FakeDB:
    """Stands in for AsyncIOMotorDatabase - db["collection_name"] indexing,
    same as every storage/*.py repository actually uses."""

    def __init__(self):
        self._collections: dict[str, FakeCollection] = {}

    def __getitem__(self, name: str) -> FakeCollection:
        return self._collections.setdefault(name, FakeCollection())


@pytest.fixture
def fake_db() -> FakeDB:
    return FakeDB()
