"""Network-free formatting / conversion helpers for the Discord connector.

Turning discord.py objects into the bridge's platform-neutral types
(`StandardMessage` / `StandardReaction`), coercing names into what the
webhook and emoji APIs accept, comparing reaction emoji, and stripping a
pasted `<#id>` / `<@&id>` mention down to its bare id - none of which need
the client.
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit

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

# Discord's built-in GIF picker (Tenor, then Klipy after Tenor's API was
# killed) doesn't post a native attachment - it posts the picker page's URL
# as plain content, which Discord auto-unfurls into an embed. Recognized by
# embed type first, falling back to a known GIF-picker host in `embed.url`
# for a picker whose embed type doesn't come through as expected (issue #102).
_GIF_EMBED_TYPES = frozenset({"gifv", "image"})
_GIF_EMBED_HOSTS = ("tenor.com", "klipy.co", "klipy.com")


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


def _member_color(author: object) -> str | None:
    """The CSS hex color of a Discord member's displayed name, or None.

    discord.py's `Member.color` already resolves to the highest role that
    has a non-default color (falling back to `Color.default()`, value 0,
    when the member has none) - so a zero value means "no color". A plain
    `discord.User` with no guild context has no `.color` and yields None.
    Forwarded to a Stoat masquerade's `color` (issue #74)."""
    value = getattr(getattr(author, "color", None), "value", 0) or 0
    return f"#{value:06x}" if value else None


def _map_mentioned_users(message: object) -> dict[str, str]:
    """Native user id -> display name for every user `message` @-mentions, for
    `StandardMessage.mentioned_users` / `StandardEdit.mentioned_users` (issue
    #56). Best-effort: a `message` with no `mentions` (a raw edit payload whose
    message couldn't be built) just yields an empty map."""
    return {str(u.id): u.display_name for u in (getattr(message, "mentions", None) or [])}


def _map_mentioned_roles(message: object) -> dict[str, str]:
    """Native role id -> role name for every role `message` @-mentions, for
    `StandardMessage.mentioned_roles` / `StandardEdit.mentioned_roles` (issue
    #4). Best-effort, same as `_map_mentioned_users`: a `message` with no
    `role_mentions` just yields an empty map."""
    return {str(r.id): r.name for r in (getattr(message, "role_mentions", None) or [])}


def _map_mentioned_channels(message: object) -> dict[str, str]:
    """Native channel id -> name for every channel `message` `<#id>`-mentions,
    for `StandardMessage.mentioned_channels` / `StandardEdit.mentioned_channels`
    (issue #84). discord.py resolves `channel_mentions` from cache, so a miss
    (or a raw edit payload with no message) just yields fewer entries and the
    receiver leaves that `<#id>` token as-is."""
    return {
        str(c.id): getattr(c, "name", str(c.id))
        for c in (getattr(message, "channel_mentions", None) or [])
    }


def _to_standard_message(
    message: discord.Message,
    connector_id: str,
    *,
    source_label: str | None = None,
    sender_pronouns: str | None = None,
    sender_color: str | None = None,
) -> StandardMessage:
    gif_embeds = _gif_embeds(message)
    content = message.content
    for embed in gif_embeds:
        if embed.url and embed.url in content:
            content = content.replace(embed.url, "").strip()
    return StandardMessage(
        origin_connector_id=connector_id,
        origin_channel_id=str(message.channel.id),
        channel_name=getattr(message.channel, "name", str(message.channel.id)),
        sender_name=message.author.display_name,
        sender_avatar_url=str(message.author.display_avatar.url) if message.author.display_avatar else None,
        sender_user_id=str(message.author.id),
        content_markdown=content,
        message_id=str(message.id),
        source_label=source_label,
        sender_pronouns=sender_pronouns,
        sender_color=sender_color,
        attachments=[
            Attachment(url=a.url, filename=a.filename, content_type=a.content_type, size_bytes=a.size)
            for a in message.attachments
        ]
        + _gif_embed_attachments(gif_embeds),
        mentioned_users=_map_mentioned_users(message),
        mentioned_roles=_map_mentioned_roles(message),
        mentioned_channels=_map_mentioned_channels(message),
        reply_to_message_id=_reply_to_message_id(message),
    )


def _reply_to_message_id(message: discord.Message) -> str | None:
    """The id of the message `message` replies to, or None if it isn't a
    reply (no reference, or a forward/other non-reply reference type -
    issue #101). `getattr` throughout since a message that isn't a reply may
    have no `reference` attribute at all (real discord.py messages always do,
    but this stays defensive the same way the mention-mapping helpers are)."""
    reference = getattr(message, "reference", None)
    if reference is None:
        return None
    ref_type = getattr(reference, "type", discord.MessageReferenceType.default)
    if ref_type not in (discord.MessageReferenceType.default, discord.MessageReferenceType.reply):
        return None
    message_id = getattr(reference, "message_id", None)
    if message_id is None:
        return None
    return str(message_id)


def _gif_embeds(message: discord.Message) -> list[discord.Embed]:
    """Every embed on `message` that looks like a GIF-picker (Tenor/Klipy)
    auto-unfurl, rather than an attachment (issue #102). `getattr` for the
    same defensiveness `_map_mentioned_users` etc. use against a bare fake in
    tests / an edit payload with no `embeds`."""
    embeds = getattr(message, "embeds", None) or []
    matched = []
    for embed in embeds:
        if embed.type in _GIF_EMBED_TYPES:
            matched.append(embed)
        elif embed.url and any(host in embed.url for host in _GIF_EMBED_HOSTS):
            matched.append(embed)
    return matched


def _gif_embed_attachments(gif_embeds: list[discord.Embed]) -> list[Attachment]:
    """Turn each GIF-picker embed into a re-uploadable `Attachment`, pointing
    at the actual playable/still asset rather than the picker webpage link -
    `services/formatting.download_attachments` + the Stoat receiver then
    handle it exactly like a native attachment (issue #102)."""
    attachments = []
    for embed in gif_embeds:
        asset_url = _gif_embed_asset_url(embed)
        if asset_url is None:
            continue
        filename, content_type = _guess_gif_asset_type(asset_url)
        attachments.append(Attachment(url=asset_url, filename=filename, content_type=content_type))
    return attachments


def _gif_embed_asset_url(embed: discord.Embed) -> str | None:
    """The directly-fetchable media URL for a GIF-picker embed - the playable
    video first (a `gifv` embed's real media is usually an .mp4, not a
    .gif), falling back to a still image if there's no video."""
    for media in (embed.video, embed.image, embed.thumbnail):
        url = getattr(media, "url", None)
        if url:
            return url
    return None


def _guess_gif_asset_type(asset_url: str) -> tuple[str, str]:
    """(filename, content_type) guessed from the asset URL's path extension,
    query string ignored. Defaults to gif/image when the extension is
    anything else (or missing) - the 8 MiB post-download size check in
    `download_attachments` still applies regardless of the guess."""
    path = urlsplit(asset_url).path.lower()
    if path.endswith(".mp4"):
        return "gif.mp4", "video/mp4"
    if path.endswith(".webm"):
        return "gif.webm", "video/webm"
    return "gif.gif", "image/gif"


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
