"""`AttachmentPreferenceManager` - `/attachments prefer|unprefer|preferences`
(issue #164).

By default a link-preview embed is carried across as the *source*
platform's resolved media, re-uploaded as an attachment. A rule here names
the platform kind (`discord`/`stoat`) that should instead rebuild previews
for matching URLs itself: that receiver skips the attachment and leaves the
raw link in the text for its own unfurler. Rules are global (keyed by kind,
not connector id) and match as a case-insensitive substring of the page URL;
the longest matching substring wins.

IRC has no link previews, so it's never a valid kind and has no command.
"""

from __future__ import annotations

from stoat_discord_bridge.admin_commands.common import LinkError
from stoat_discord_bridge.services.caching import AsyncTTLCache
from stoat_discord_bridge.storage.attachment_preferences import (
    AttachmentPreference,
    AttachmentPreferenceRepository,
)

PREFERABLE_KINDS = ("discord", "stoat")

# Every relayed link preview checks the rules, so keep the Mongo read off
# that path. This manager's own writes invalidate it immediately.
_CACHE_TTL = 30.0
_CACHE_KEY = "rules"


class AttachmentPreferenceManager:
    def __init__(self, repo: AttachmentPreferenceRepository) -> None:
        self._repo = repo
        self._cache: AsyncTTLCache[list[AttachmentPreference]] = AsyncTTLCache(_CACHE_TTL)

    async def preferred_kind_for(self, url: str) -> str | None:
        """The kind that should rebuild `url`'s preview itself, or None if no
        rule matches (use the source's media)."""
        haystack = url.casefold()
        best: AttachmentPreference | None = None
        for rule in await self._cache.get(_CACHE_KEY, self._load):
            if rule.url_substring in haystack and (best is None or len(rule.url_substring) > len(best.url_substring)):
                best = rule
        return best.preferred_kind if best is not None else None

    async def _load(self, _key: str) -> list[AttachmentPreference]:
        return await self._repo.list_all()

    async def prefer(self, *, kind: str, url_substring: str) -> str:
        """`/attachments prefer <kind> <url-substr>`. Raises LinkError on an
        unknown kind or a blank substring."""
        kind = kind.strip().casefold()
        if kind not in PREFERABLE_KINDS:
            raise LinkError(f"kind must be one of: {', '.join(PREFERABLE_KINDS)}.")
        substring = _clean_substring(url_substring)
        created = await self._repo.set(substring, kind)
        self._cache.invalidate(_CACHE_KEY)
        suffix = "" if created else " (replaced the previous rule)"
        return f"Links containing '{substring}' will now use {kind}'s own preview{suffix}."

    async def unprefer(self, *, url_substring: str) -> str:
        """`/attachments unprefer <url-substr>`. Raises LinkError if no rule
        exists for it."""
        substring = _clean_substring(url_substring)
        removed = await self._repo.remove(substring)
        self._cache.invalidate(_CACHE_KEY)
        if not removed:
            raise LinkError(f"there's no preference for '{substring}'.")
        return f"Removed the preference for '{substring}'."

    async def list_preferences(self) -> str:
        """`/attachments preferences` - read-only."""
        rules = sorted(await self._repo.list_all(), key=lambda r: r.url_substring)
        if not rules:
            return "No attachment preferences are set."
        return "Attachment preferences:\n" + "\n".join(f"{r.url_substring} -> {r.preferred_kind}" for r in rules)


def _clean_substring(url_substring: str) -> str:
    substring = url_substring.strip().casefold()
    if not substring:
        raise LinkError("the URL substring can't be empty.")
    return substring
