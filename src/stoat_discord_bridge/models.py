"""Standardized message format used to move a single chat message between
platform-specific sender/receiver services.

A *sender* service listens to one endpoint (a configured Discord/Stoat/IRC
connector) and turns native events into a `StandardMessage`. A *receiver*
service takes a `StandardMessage` and posts it into one endpoint, handling
whatever platform-specific quirks that requires (splitting long messages,
stripping markdown, re-uploading attachments as native files on Discord/Stoat
or inlining their URLs on IRC, etc.).
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
    # The origin connector's display label (config.yaml's `label` - e.g.
    # "Discord", "Stoat (public)", "IRC"), stamped by every sender. A receiver
    # whose connector has `source_forwarding` on folds it into the displayed
    # sender identity (Discord webhook username / Stoat masquerade name /
    # IRC `<nick>` prefix) so a relayed message shows where it came from.
    # None only for a message a sender built before this field existed.
    source_label: str | None = None
    # The sender's pronouns as free text ("she/her"), resolved best-effort by
    # the origin sender from the platform's profile - Discord's profile
    # endpoint, Stoat's per-server-then-account profile - when that connector
    # has `pronoun_forwarding` on. None where unknown, unavailable, or the
    # platform has no such concept (IRC always). A receiver whose connector
    # has `pronoun_forwarding` on shows it alongside `source_label`.
    sender_pronouns: str | None = None
    # Native user id -> display name on the origin, for every user the message
    # @-mentions. Lets a receiver expand a `<@id>` mention of a user who ISN'T
    # /link-user-linked on the target into a readable `@Display Name` instead
    # of relaying the raw id token (issue #56). Best-effort: a sender that
    # can't resolve a name (cache miss) or a connector with no structured
    # mentions (IRC) just leaves the entry / whole map absent.
    mentioned_users: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ChannelMetadata:
    """A bridged channel's cosmetic properties, read off the source channel
    when `/mirror channel` (or thread / linked-Category auto-mirror) creates
    its counterpart so the new channel isn't left blank - see
    `admin_commands.ChannelLinker.mirror_channel` and each connector's
    `ensure_channel` hook, which applies these *only on the create path*
    (a mirror that reuses an existing channel leaves its metadata alone).

    `icon_url` is only ever populated for a Stoat source - Discord guild
    text channels have no per-channel icon - and only Stoat's
    `ensure_channel` consumes it; IRC ignores the whole struct.
    """

    description: str | None = None
    nsfw: bool = False
    icon_url: str | None = None


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
class StandardPin:
    """A message pin/unpin event, in the platform-neutral shape senders/
    receivers pass around. Relayed only onto connectors that advertise
    `ReceiverService.supports_pins` and only for a message the bridge
    previously relayed (tracked via MessageSyncRepository) — see
    BridgeCoordinator.handle_pin."""

    origin_connector_id: ConnectorId
    origin_channel_id: str
    origin_message_id: str  # the message being (un)pinned, native id on the origin platform
    pinned: bool  # True = pinned, False = unpinned


@dataclass(frozen=True)
class StandardTyping:
    """A "user started typing" event, in the platform-neutral shape senders/
    receivers pass around. Relayed only onto connectors that advertise
    `ReceiverService.supports_typing` and only for a channel the bridge has a
    mapping for — see BridgeCoordinator.handle_typing. Fire-and-forget: no
    message id, no sync tracking, nothing recorded. `sender_name` is
    best-effort and only cosmetic — neither Discord nor Stoat can attribute a
    relayed typing indicator to anyone but the bridge bot itself.

    `active` is False for an explicit "stopped typing" event (Stoat's
    `channel_stop_typing`); a receiver that can clear an indicator early
    (Stoat) does so, one that can't (Discord) lets it lapse on its own."""

    origin_connector_id: ConnectorId
    origin_channel_id: str
    sender_name: str
    sender_user_id: str
    active: bool = True


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
