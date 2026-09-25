"""Per-URL-substring link-preview preferences (issue #164) - which platform
kind (`discord`/`stoat`) should rebuild a link's preview itself instead of
receiving the source platform's resolved media as a re-uploaded attachment.
Populated by the `/attachments prefer` admin command (see
`admin_commands.attachment_preferences.AttachmentPreferenceManager`).

One document per `url_substring`; rules are global, not per-connector.
"""

from __future__ import annotations

from dataclasses import dataclass

from motor.motor_asyncio import AsyncIOMotorDatabase


@dataclass(frozen=True)
class AttachmentPreference:
    url_substring: str
    preferred_kind: str  # "discord" or "stoat"


class AttachmentPreferenceRepository:
    def __init__(self, db: AsyncIOMotorDatabase) -> None:
        self._collection = db["attachment_preferences"]

    async def ensure_indexes(self) -> None:
        await self._collection.create_index([("url_substring", 1)], unique=True)

    async def set(self, url_substring: str, preferred_kind: str) -> bool:
        """Upsert a rule. Returns True if it's new, False if an existing rule
        for `url_substring` was overwritten."""
        result = await self._collection.update_one(
            {"url_substring": url_substring},
            {"$set": {"url_substring": url_substring, "preferred_kind": preferred_kind}},
            upsert=True,
        )
        return result.upserted_id is not None

    async def remove(self, url_substring: str) -> bool:
        """Returns False if there was no rule for `url_substring`."""
        result = await self._collection.delete_one({"url_substring": url_substring})
        return result.deleted_count > 0

    async def list_all(self) -> list[AttachmentPreference]:
        return [
            AttachmentPreference(url_substring=doc["url_substring"], preferred_kind=doc["preferred_kind"])
            async for doc in self._collection.find({})
        ]
