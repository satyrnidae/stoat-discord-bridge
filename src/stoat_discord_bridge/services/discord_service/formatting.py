"""Network-free formatting / conversion helpers for the Discord connector.

Turning discord.py objects into the bridge's platform-neutral types
(`StandardMessage` / `StandardReaction`), coercing names into what the
webhook and emoji APIs accept, comparing reaction emoji, and stripping a
pasted `<#id>` / `<@&id>` mention down to its bare id - none of which need
the client.
"""

from __future__ import annotations

import re

import discord

from stoat_discord_bridge.models import Attachment, CustomEmoji, StandardMessage, StandardReaction
from stoat_discord_bridge.services.role_sync import NEUTRAL_PERMISSIONS

# Discord webhook hard limits: 2000 chars per message, 1-80 char usernames,
# and usernames may not contain "clyde" or "discord" (case-insensitive) or
# the API rejects the send outright.
_CONTENT_LIMIT = 2000
_USERNAME_LIMIT = 80
_FORBIDDEN_USERNAME_SUBSTRINGS = ("clyde", "discord")

# The discord.py PermissionOverwrite attribute names that role permission
# mirroring is allowed to touch - every other bit on a target overwrite is
# preserved as-is (see sync.set_channel_role_permission).
_MAPPED_DISCORD_PERM_ATTRS = {d_attr for d_attr, _ in NEUTRAL_PERMISSIONS.values()}

_CHANNEL_MENTION_RE = re.compile(r"^<#(\d+)>$")
_ROLE_MENTION_RE = re.compile(r"^<@&(\d+)>$")


def _normalize_role_id(raw: str) -> str:
    """Strip a pasted `<@&id>` role mention down to the bare id; leave a bare
    id or a role name untouched (RoleLinker resolves a name itself)."""
    match = _ROLE_MENTION_RE.match(raw.strip())
    return match.group(1) if match else raw.strip()


def _normalize_channel_id(raw: str) -> str:
    """The `external_id`/`local_id` slash-command
    options below are plain strings, not discord.py channel-type options -
    Discord's client still lets a user pick a channel from the `#` picker
    while typing one, which pastes a full `<#id>` mention rather than the
    bare id. Strip that down to the id so it's actually usable as one -
    otherwise it ends up stored as a channel_id that never matches a real
    incoming message's origin_channel_id, and (for /mirror channel, which
    also uses this as the display name when no name can be resolved) as the
    literal name of the channel created on the other connector."""
    match = _CHANNEL_MENTION_RE.match(raw.strip())
    return match.group(1) if match else raw


def _map_mentioned_users(message: object) -> dict[str, str]:
    """Native user id -> display name for every user `message` @-mentions, for
    `StandardMessage.mentioned_users` / `StandardEdit.mentioned_users` (issue
    #56). Best-effort: a `message` with no `mentions` (a raw edit payload whose
    message couldn't be built) just yields an empty map."""
    return {str(u.id): u.display_name for u in (getattr(message, "mentions", None) or [])}


def _to_standard_message(
    message: discord.Message,
    connector_id: str,
    *,
    source_label: str | None = None,
    sender_pronouns: str | None = None,
) -> StandardMessage:
    return StandardMessage(
        origin_connector_id=connector_id,
        origin_channel_id=str(message.channel.id),
        channel_name=getattr(message.channel, "name", str(message.channel.id)),
        sender_name=message.author.display_name,
        sender_avatar_url=str(message.author.display_avatar.url) if message.author.display_avatar else None,
        sender_user_id=str(message.author.id),
        content_markdown=message.content,
        message_id=str(message.id),
        source_label=source_label,
        sender_pronouns=sender_pronouns,
        attachments=[
            Attachment(url=a.url, filename=a.filename, content_type=a.content_type, size_bytes=a.size)
            for a in message.attachments
        ],
        mentioned_users=_map_mentioned_users(message),
    )


def _to_standard_reaction(
    payload: discord.RawReactionActionEvent, connector_id: str, *, added: bool, reactor_count: int | None = None
) -> StandardReaction:
    emoji = payload.emoji
    emoji_repr: str | CustomEmoji
    if emoji.is_custom_emoji():
        emoji_repr = CustomEmoji(
            native_id=str(emoji.id), name=emoji.name or "", image_url=str(emoji.url), animated=emoji.animated
        )
    else:
        emoji_repr = emoji.name  # plain unicode emoji
    return StandardReaction(
        origin_connector_id=connector_id,
        origin_channel_id=str(payload.channel_id),
        origin_message_id=str(payload.message_id),
        emoji=emoji_repr,
        added=added,
        origin_reactor_count=reactor_count,
    )


def _to_discord_emoji(emoji: str | CustomEmoji) -> str | discord.PartialEmoji:
    if isinstance(emoji, str):
        return emoji
    return discord.PartialEmoji(name=emoji.name, id=int(emoji.native_id), animated=emoji.animated)


def _discord_reaction_matches(existing: object, want: object) -> bool:
    """Whether `existing` (a `discord.Reaction.emoji` - str, Emoji, or
    PartialEmoji) is the same emoji as `want` (a str or PartialEmoji from
    `_to_discord_emoji` / a raw payload). Custom emoji compare by id;
    unicode by string."""
    want_id = getattr(want, "id", None)
    existing_id = getattr(existing, "id", None)
    if want_id is not None or existing_id is not None:
        return want_id is not None and existing_id is not None and int(want_id) == int(existing_id)
    return str(existing) == str(want)


def _sanitize_emoji_name(name: str) -> str:
    """Coerce an inbound emoji name into Discord's rules: 2-32 chars, alphanumeric/underscore only."""
    name = re.sub(r"[^A-Za-z0-9_]", "_", name).strip("_") or "emoji"
    name = name[:32]
    return name if len(name) >= 2 else name.ljust(2, "_")


def _sanitize_username(name: str) -> str:
    """Coerce an inbound display name into something the webhook API will accept."""
    name = name.strip() or "Unknown User"
    for forbidden in _FORBIDDEN_USERNAME_SUBSTRINGS:
        name = re.sub(re.escape(forbidden), "*" * len(forbidden), name, flags=re.IGNORECASE)
    return name[:_USERNAME_LIMIT]
