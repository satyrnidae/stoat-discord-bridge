"""Standardized message format used to move a single chat message between
platform-specific sender/receiver services.

A *sender* service listens to one endpoint (a configured Discord/Stoat/IRC
connector) and turns native events into a `StandardMessage`. A *receiver*
service takes a `StandardMessage` and posts it into one endpoint, handling
whatever platform-specific quirks that requires (splitting long messages,
stripping markdown, turning attachments into inline URLs, etc.) — see the
TODOs in `services/`.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# The id of one configured connector (a single Discord guild, Stoat server,
# or IRC network) from config.yaml - free-form, operator-chosen, unique
# across every connector regardless of kind. Not an enum: any number of
# connectors of each kind can be configured, so this is no longer a fixed set.
ConnectorId = str


@dataclass(frozen=True)
class Attachment:
    url: str
    filename: str | None = None
    content_type: str | None = None
    size_bytes: int | None = None


@dataclass(frozen=True)
class StandardMessage:
    """One chat message, in the platform-neutral shape senders/receivers pass around."""

    origin_connector_id: ConnectorId
    origin_channel_id: str
    channel_name: str
    sender_name: str  # display name / username / nickname, whichever the origin platform calls it
    sender_avatar_url: str | None
    # Native user ID of the sender on origin_connector_id - for IRC this IS
    # the nick (same convention as storage/user_mappings.py's UserMapping.user_id).
    # Used to look up a /link-user mapping so a linked sender's masquerade on
    # the target connector can show the locally-linked identity instead of
    # the remote one (see each receiver's receive()).
    sender_user_id: str
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
    """A custom emoji newly added on `origin_connector_id`, for the bridge to
    mirror onto every other connector (see BridgeCoordinator.handle_emoji_created)."""

    origin_connector_id: ConnectorId
    emoji: CustomEmoji


@dataclass(frozen=True)
class StandardEmojiDeleted:
    """A custom emoji removed on `origin_connector_id`. Deletions are NOT
    mirrored onto other connectors — a copy still in use elsewhere keeps
    working there. This only tells the bridge to drop `origin_connector_id`'s
    entry from EmojiMappingRepository's bookkeeping (see
    BridgeCoordinator.handle_emoji_deleted)."""

    origin_connector_id: ConnectorId
    native_id: str


@dataclass(frozen=True)
class StandardReaction:
    """A reaction add/remove event, in the platform-neutral shape senders/
    receivers pass around. `emoji` is a bare unicode string (universal across
    platforms) or a `CustomEmoji` (needs ID translation via EmojiMappingRepository)."""

    origin_connector_id: ConnectorId
    origin_channel_id: str
    origin_message_id: str
    emoji: str | CustomEmoji
    added: bool  # True = reaction added, False = reaction removed
    # How many users still hold this emoji on the ORIGIN message after this
    # event (includes the acting user on an add). Lets BridgeCoordinator skip
    # a mirrored add when someone else already reacted with it, and hold the
    # mirrored reaction until the last origin user removes theirs. None =
    # the origin couldn't tell us - the coordinator then acts best-effort.
    origin_reactor_count: int | None = None
