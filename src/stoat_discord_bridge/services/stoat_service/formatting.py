"""Network-free formatting / conversion helpers for the Stoat connector.

Turning stoat.py's object shapes into the bridge's platform-neutral types
(display name, avatar URL, attachments), classifying a reaction emoji
string, and the outbound content length cap - none of which need the
client.
"""

from __future__ import annotations

import re

import aiohttp

from stoat_discord_bridge.models import Attachment, CustomEmoji

# Stoat message length cap (matches Discord's 2000-char webhook limit; stoat.py
# doesn't expose its own constant, so this mirrors the documented server-side max).
_CONTENT_LIMIT = 2000


def _channel_category(channel):
    """Best-effort `channel.category` read - the property can raise NoData
    on a cache miss (same caveat as StoatSenderService.get_channel_category_
    name), so command handlers that need "the Category this channel is in"
    go through this rather than touching `.category` directly."""
    try:
        return channel.category
    except Exception:
        return None


def _channel_server_id(channel) -> str | None:
    """The id of the server a message's channel belongs to, or None for a DM /
    a partial channel that hasn't got it populated. Central enough to the
    sender's fetch-a-fresh-member fallbacks (name / avatar / pronouns) to
    share rather than re-spell `getattr(..., "server_id", None)` at each."""
    return getattr(channel, "server_id", None)


def _display_name(author) -> str:
    """Best-effort display name for a Stoat message author/member.

    stoat.py's `Member.display_name` property - confirmed against the
    installed package (server.py) - passes straight through to the
    underlying User's *account-level* display_name and never reads the
    member's own per-server `nick` field at all, even though `nick` is a
    distinct attribute the same Member carries. Left unchecked, that means a
    member with a server nickname set but no account-level display name
    falls all the way through to `tag` (username#discriminator) - showing a
    raw username where the nickname should appear. So check `nick` first,
    mirroring the same per-server-override-before-global preference already
    given to avatars by `_avatar_url` below.

    Falls back to the bare `name` (not `tag`) when neither is set - a
    masquerade name showing a bare `#0000`-style discriminator suffix reads
    as broken/internal even though it's technically accurate, so it's
    stripped here rather than carried through to whatever's displaying the
    masquerade."""
    return getattr(author, "nick", None) or getattr(author, "display_name", None) or author.name


def _avatar_url(author) -> str | None:
    """Best-effort avatar URL for a Stoat message author. stoat.py exposes
    an avatar as an `Asset` (a `.url()` *method*, not a plain string
    attribute - there's no `avatar_url` shortcut on User/Member, confirmed
    against the installed stoat.py package directly), and a Member's
    optional per-server avatar override takes priority over the
    account-level one when set, matching how it's displayed in the client.
    Falls back to the platform's default avatar if the author has neither."""
    asset = getattr(author, "server_avatar", None) or getattr(author, "avatar", None)
    if asset is not None:
        return asset.url()
    return getattr(author, "default_avatar_url", None)


def _member_colour(author) -> str | None:
    """Best-effort CSS colour of a Stoat member's displayed name - the colour
    of their highest-priority coloured role, matching how the client tints it.

    `Member.roles` is cache-dependent (needs the server and its roles cached);
    a miss, a member with no coloured role, or a non-member author all yield
    None. Stoat role `rank` is ascending-priority (lower rank = higher), so
    the lowest-rank coloured role wins. The role colour is already a CSS
    string (Stoat allows gradients), passed straight through to a masquerade's
    `color` (issue #74)."""
    try:
        coloured = [r for r in (getattr(author, "roles", None) or []) if getattr(r, "color", None)]
        coloured.sort(key=lambda r: getattr(r, "rank", 0))
    except Exception:
        return None
    return coloured[0].color if coloured else None


def _extract_pronouns(data) -> str | None:
    """Pull a pronoun string out of a raw Stoat user / member / profile JSON
    payload. stoat.py 1.2.1 models no such field, so a deployment that has one
    can put it either at the top level (`pronouns`) or inside a nested
    `profile` object - check both. Anything unexpected -> None."""
    if not isinstance(data, dict):
        return None
    profile = data.get("profile")
    candidates = [data.get("pronouns")]
    if isinstance(profile, dict):
        candidates.append(profile.get("pronouns"))
    for candidate in candidates:
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    return None


def _map_attachments(message) -> list[Attachment]:
    """Map stoat.py's `Message.attachments` (a list of `stoat.cdn.Asset` -
    each with a `.url()` *method*, plus `.filename` / `.content_type` /
    `.size`) onto the bridge's platform-neutral `Attachment`. Defensive
    `getattr`/`try` throughout, same as the rest of this module treats
    stoat.py's shapes as unverified against a live server."""
    out: list[Attachment] = []
    for a in getattr(message, "attachments", None) or []:
        try:
            url_attr = getattr(a, "url", None)
            url = url_attr() if callable(url_attr) else url_attr
        except Exception:
            url = None
        if not url:
            continue
        out.append(
            Attachment(
                url=url,
                filename=getattr(a, "filename", None),
                content_type=getattr(a, "content_type", None),
                size_bytes=getattr(a, "size", None),
            )
        )
    return out


def _map_mentioned_users(message) -> dict[str, str]:
    """Native user id -> display name for every user `message` @-mentions, for
    `StandardMessage.mentioned_users` (issue #56). Best-effort: stoat.py's
    `Message.mentions` is cache-dependent and quietly returns fewer entries
    (or none) on a cache miss - a mention we can't name just isn't in the map
    and the receiver leaves that `<@id>` token as-is."""
    out: dict[str, str] = {}
    try:
        mentions = message.mentions
    except Exception:
        mentions = []
    for member in mentions or []:
        try:
            out[str(member.id)] = _display_name(member)
        except Exception:
            continue
    return out


def _map_mentioned_roles(message) -> dict[str, str]:
    """Native role id -> role name for every role `message` @-mentions, for
    `StandardMessage.mentioned_roles` (issue #4). Best-effort, same caveat as
    `_map_mentioned_users`: stoat.py's `Message.role_mentions` is
    cache-dependent and quietly returns fewer entries (or none) on a cache
    miss - a role we can't name just isn't in the map and the receiver leaves
    that `<%id>` token as-is."""
    out: dict[str, str] = {}
    try:
        roles = message.role_mentions
    except Exception:
        roles = []
    for role in roles or []:
        try:
            out[str(role.id)] = role.name
        except Exception:
            continue
    return out


async def _download(url: str) -> bytes:
    async with aiohttp.ClientSession() as session, session.get(url) as resp:
        resp.raise_for_status()
        return await resp.read()


_ULID_RE = re.compile(r"[0-9A-Za-z]{26}")


def _parse_stoat_emoji(emoji_id: str) -> str | CustomEmoji | None:
    """Classify a stoat reaction emoji string (`MessageReactEvent.emoji`):

    - a real Unicode emoji (has non-ASCII codepoints) -> passed straight
      through; every connector understands it.
    - a 26-char base32 ULID -> a server custom emoji; translated to the
      target's linked copy via EmojiMappingRepository downstream.
    - anything else - an ASCII shortcode like `distorted_face`, `trollface`
      - is a Stoat/Revolt *builtin* emoji pack entry with no Unicode
      codepoint and no cross-platform equivalent. Return None so the caller
      drops the reaction rather than relaying the literal text.
    """
    if not emoji_id.isascii():
        return emoji_id
    if _ULID_RE.fullmatch(emoji_id):
        return CustomEmoji(native_id=emoji_id, name="", image_url="")
    return None


def _to_stoat_emoji(emoji: str | CustomEmoji) -> str:
    return emoji if isinstance(emoji, str) else emoji.native_id
