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
    # Set when this attachment is media resolved from a link-preview embed
    # rather than a real upload: the web page URL the preview was built from.
    # Each receiver uses it to strip the link from the text, or to drop the
    # attachment and let its own platform unfurl the link (issue #164).
    source_page_url: str | None = None
    # An image's alt text (issue #188). Only Discord has one - stoat.py's
    # asset model has no equivalent, so it's always None from Stoat.
    description: str | None = None


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
    # the origin sender from the platform's profile (Stoat's
    # per-server-then-account profile) when that connector has
    # `pronoun_forwarding` on. None where unknown, unavailable, or the
    # platform has no bot-accessible pronoun field - IRC always, and Discord
    # (its profile endpoint is 403-blocked for bots, issue #58). A receiver
    # whose connector has `pronoun_forwarding` on shows it alongside
    # `source_label`.
    sender_pronouns: str | None = None
    # The sender's displayed name color as a CSS color string ("#5865f2",
    # and any other valid CSS color a platform allows - Stoat role colors
    # can be gradients), resolved best-effort by the origin sender from the
    # sender's top colored role when that connector has `color_forwarding`
    # on. None where the sender has no color, the platform has no user-color
    # concept (IRC always), or it couldn't be resolved. Only Stoat's receiver
    # consumes it - a masquerade carries a `color` (issue #74); Discord
    # webhooks and IRC can't tint a relayed name.
    sender_color: str | None = None
    # Native user id -> display name on the origin, for every user the message
    # @-mentions. Lets a receiver expand a `<@id>` mention of a user who ISN'T
    # /link-user-linked on the target into a readable `@Display Name` instead
    # of relaying the raw id token (issue #56). Best-effort: a sender that
    # can't resolve a name (cache miss) or a connector with no structured
    # mentions (IRC) just leaves the entry / whole map absent.
    mentioned_users: dict[str, str] = field(default_factory=dict)
    # Native role id -> role name on the origin, for every role the message
    # @-mentions. The role counterpart of `mentioned_users` (issue #4): lets a
    # receiver expand a `<@&id>` (Discord) / `<%id>` (Stoat) mention of a role
    # that ISN'T /link-role-linked on the target into a readable `@Role Name`
    # instead of relaying the raw id token. Best-effort: a sender that can't
    # name a role, or a connector with no role concept (IRC), just leaves the
    # entry / whole map absent.
    mentioned_roles: dict[str, str] = field(default_factory=dict)
    # Native channel id -> channel name on the origin, for every channel the
    # message `<#id>`-mentions. Lets a receiver expand a mention of a channel
    # that ISN'T /link-channel-linked on the target into a readable
    # `#channel-name` instead of relaying the raw `<#id>` token, which renders
    # as a dead id there (issue #84 - the channel-level counterpart of #56).
    # Best-effort: a name a sender can't resolve, or a connector with no
    # structured channel mentions (IRC), just leaves the entry / map absent.
    mentioned_channels: dict[str, str] = field(default_factory=dict)
    # Native custom-emoji id -> name on the origin, for every inline custom
    # emoji the message's text references. Discord's `<:name:id>` token
    # already carries its name, so only a Stoat-origin sender populates this
    # (its `:ULID:` token doesn't) - lets a receiver fall back to a readable
    # `:name:` shortcode for an emoji with no link to the target instead of
    # relaying the bare id (issue #87, the emoji counterpart of #56/#4/#84).
    # Best-effort: an id a sender can't name just leaves the entry absent.
    mentioned_emoji: dict[str, str] = field(default_factory=dict)
    # The origin connector's native id of the message THIS message is a reply
    # to, unresolved to any target yet - the coordinator resolves it to each
    # target's own counterpart (via MessageSyncRepository) before calling a
    # receiver's `receive()`. None when the message isn't a reply, or the
    # origin platform's reply reference couldn't be read (issue #101).
    # Currently populated by the Discord sender only.
    reply_to_message_id: str | None = None


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

    `slowmode_delay` is in seconds (Discord's and Stoat's native units agree,
    so no conversion is needed) - populated for a Discord source from
    `TextChannel.slowmode_delay`, for a Stoat source via a raw channel fetch
    (stoat.py's typed client doesn't model the field), and `None` when the
    source had no slowmode or it couldn't be read. IRC has no slowmode
    concept and ignores it (issue #108).
    """

    description: str | None = None
    nsfw: bool = False
    icon_url: str | None = None
    slowmode_delay: int | None = None


@dataclass(frozen=True)
class RoleMetadata:
    """A linked role's cosmetic properties, read off the source role when
    `/mirror role` creates its counterpart (issue #179) - the role
    counterpart of `ChannelMetadata`, applied by each connector's
    `ensure_role` hook *only on the create path*.

    `color` is a CSS color string (`#rrggbb` from Discord; Stoat's own
    `Role.color` as-is, which may be a gradient Discord can't take - its
    `ensure_role` then just skips the color). `hoist` is "display members
    separately", which both platforms have. Permissions aren't carried over.
    """

    color: str | None = None
    hoist: bool = False


@dataclass(frozen=True)
class CustomEmoji:
    """A custom (non-unicode) emoji. A plain unicode emoji needs no platform
    ID translation, so it's passed around as a bare `str` instead of this."""

    native_id: str
    name: str
    image_url: str
    animated: bool = False


@dataclass(frozen=True)
class EmojiCapacity:
    """A destination's remaining custom-emoji slots (issue #157), read where
    the client library exposes it (Discord only - see
    `ConnectorInfo.emoji_capacity`). Static and animated emoji occupy
    separate pools of equal size on Discord, so this is two numbers, not
    one."""

    free_static: int
    free_animated: int


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
    `ReceiverService.supports_pins` and only when `origin_connector_id`/
    `origin_channel_id`/`origin_message_id` is the sync group's recorded
    *origin* (tracked via MessageSyncRepository) — one-way: a pin/unpin
    performed directly on a relayed copy stays local to that platform and
    isn't mirrored back to the origin or across to other copies. See
    BridgeCoordinator.handle_pin."""

    origin_connector_id: ConnectorId
    origin_channel_id: str
    origin_message_id: str  # the message being (un)pinned, native id on the origin platform
    pinned: bool  # True = pinned, False = unpinned


@dataclass(frozen=True)
class StandardEdit:
    """A message *content* edit on `origin_connector_id`, in the platform-
    neutral shape senders/receivers pass around. Relayed onto every other
    connector's copy of the same message (tracked via MessageSyncRepository)
    for connectors that advertise `ReceiverService.supports_edits` — Discord ⇄
    Stoat only, IRC has no edit-in-place. See BridgeCoordinator.handle_edit.

    `mentioned_users` / `mentioned_roles` / `mentioned_channels` /
    `mentioned_emoji` mirror the same-named `StandardMessage` fields — the
    origin's name for every user / role / channel / custom emoji the *edited*
    text mentions — so a receiver can re-expand an unlinked `<@id>` / `<@&id>`
    / `<#id>` / emoji token the same way the original relay did."""

    origin_connector_id: ConnectorId
    origin_channel_id: str
    origin_message_id: str  # the message being edited, native id on the origin platform
    new_content_markdown: str
    mentioned_users: dict[str, str] = field(default_factory=dict)
    mentioned_roles: dict[str, str] = field(default_factory=dict)
    mentioned_channels: dict[str, str] = field(default_factory=dict)
    mentioned_emoji: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class StandardDelete:
    """A message deletion on `origin_connector_id`, in the platform-neutral
    shape senders/receivers pass around. Carries only identity - no content
    is needed to delete something. Relayed onto every other connector's copy
    of the same message (tracked via MessageSyncRepository) for connectors
    that advertise `ReceiverService.supports_deletes` — Discord ⇄ Stoat only,
    IRC has no delete-in-place. Unlike edit/pin sync, this must NOT cascade
    from a relayed copy - only a delete reported for the sync group's
    recorded *origin* fans out; see BridgeCoordinator.handle_delete."""

    origin_connector_id: ConnectorId
    origin_channel_id: str
    origin_message_id: str  # the message being deleted, native id on the origin platform


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
