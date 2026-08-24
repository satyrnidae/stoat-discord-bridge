"""Standardized message format used to move a single chat message between
platform-specific sender/receiver services.

A *sender* service listens to one endpoint (Discord, Stoat, IRC) and turns
native events into a `StandardMessage`. A *receiver* service takes a
`StandardMessage` and posts it into one endpoint, handling whatever
platform-specific quirks that requires (splitting long messages, stripping
markdown, turning attachments into inline URLs, etc.) — see the TODOs in
`services/`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Platform(str, Enum):
    DISCORD = "discord"
    STOAT_PUBLIC = "stoat_public"
    STOAT_SELFHOSTED = "stoat_selfhosted"
    IRC = "irc"


@dataclass(frozen=True)
class Attachment:
    url: str
    filename: str | None = None
    content_type: str | None = None
    size_bytes: int | None = None


@dataclass(frozen=True)
class StandardMessage:
    """One chat message, in the platform-neutral shape senders/receivers pass around."""

    origin_platform: Platform
    origin_channel_id: str
    channel_name: str
    sender_name: str  # display name / username / nickname, whichever the origin platform calls it
    sender_avatar_url: str | None
    content_markdown: str
    message_id: str  # native message ID on the origin platform, for sync tracking
    attachments: list[Attachment] = field(default_factory=list)


@dataclass(frozen=True)
class CustomEmoji:
    """A custom (non-unicode) emoji. A plain unicode emoji needs no platform
    ID translation, so it's passed around as a bare `str` instead of this."""

    native_id: str
    name: str
    image_url: str
    animated: bool = False


@dataclass(frozen=True)
class StandardEmojiCreated:
    """A custom emoji newly added on `origin_platform`, for the bridge to
    mirror onto every other platform (see BridgeCoordinator.handle_emoji_created)."""

    origin_platform: Platform
    emoji: CustomEmoji


@dataclass(frozen=True)
class StandardEmojiDeleted:
    """A custom emoji removed on `origin_platform`. Deletions are NOT mirrored
    onto other platforms — a copy still in use elsewhere keeps working there.
    This only tells the bridge to drop `origin_platform`'s entry from
    EmojiMappingRepository's bookkeeping (see BridgeCoordinator.handle_emoji_deleted)."""

    origin_platform: Platform
    native_id: str


@dataclass(frozen=True)
class StandardReaction:
    """A reaction add/remove event, in the platform-neutral shape senders/
    receivers pass around. `emoji` is a bare unicode string (universal across
    platforms) or a `CustomEmoji` (needs ID translation via EmojiMappingRepository)."""

    origin_platform: Platform
    origin_channel_id: str
    origin_message_id: str
    emoji: str | CustomEmoji
    added: bool  # True = reaction added, False = reaction removed
