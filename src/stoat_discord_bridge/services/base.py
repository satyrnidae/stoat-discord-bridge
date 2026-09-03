"""Base classes for the per-connector sender/receiver services.

Each bridged connector (a configured Discord guild, Stoat server, or IRC
network - any number of each, per config.yaml) gets:
  - a *sender*, which listens to the connector and turns native events into
    `StandardMessage`s (via the `on_message` callback given at construction)
  - a *receiver*, which takes a `StandardMessage` and posts it into the
    connector, returning the native message ID of what it posted

Platform-specific particularities (markdown stripping, attachment URL
insertion, message-length splitting, etc.) belong in each receiver's
`receive()` — see the TODOs in the per-connector-kind modules in this
package.

Reactions and custom emoji are optional receiver capabilities, not every
connector kind's concern (IRC has neither) — a receiver opts in by setting
`supports_reactions` / `supports_emoji` and overriding the relevant
method(s); `BridgeCoordinator` checks those flags before calling in, so the
default (unsupported, method raises) is never reached for a connector kind
that doesn't advertise support.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable

from stoat_discord_bridge.models import (
    CustomEmoji,
    StandardEdit,
    StandardEmojiCreated,
    StandardEmojiDeleted,
    StandardMessage,
    StandardPin,
    StandardReaction,
    StandardTyping,
)

OnMessage = Callable[[StandardMessage], Awaitable[None]]
OnReaction = Callable[[StandardReaction], Awaitable[None]]
OnEdit = Callable[[StandardEdit], Awaitable[None]]
OnPin = Callable[[StandardPin], Awaitable[None]]
OnTyping = Callable[[StandardTyping], Awaitable[None]]
OnEmojiCreated = Callable[[StandardEmojiCreated], Awaitable[None]]
OnEmojiDeleted = Callable[[StandardEmojiDeleted], Awaitable[None]]
# (origin_connector_id, user_id, added_role_ids, removed_role_ids) - a linked
# user's role set changed on one connector; see bridge.py's RoleSyncCoordinator.
OnMemberRolesChanged = Callable[[str, str, set[str], set[str]], Awaitable[None]]
# (origin_connector_id, role_id, new_name) - a role was renamed on one connector.
OnRoleRenamed = Callable[[str, str, str], Awaitable[None]]
# (origin_connector_id, role_id) - a role was deleted on one connector.
OnRoleDeleted = Callable[[str, str], Awaitable[None]]
# (origin_connector_id, channel_id, role_id, RolePermissionOverride, *, is_category)
# - a linked role's permission override on a channel/category changed.
OnChannelRolePermissionChanged = Callable[..., Awaitable[None]]


class SenderService(ABC):
    """Listens to one connector and emits StandardMessages for the bridge to relay."""

    connector_id: str

    def __init__(
        self,
        on_message: OnMessage,
        on_reaction: OnReaction | None = None,
        on_emoji_created: OnEmojiCreated | None = None,
        on_emoji_deleted: OnEmojiDeleted | None = None,
        on_pin: OnPin | None = None,
        on_typing: OnTyping | None = None,
        on_edit: OnEdit | None = None,
    ) -> None:
        self._on_message = on_message
        self._on_reaction = on_reaction
        self._on_emoji_created = on_emoji_created
        self._on_emoji_deleted = on_emoji_deleted
        self._on_pin = on_pin
        self._on_typing = on_typing
        self._on_edit = on_edit

    @abstractmethod
    async def start(self) -> None:
        """Connect and begin listening. Runs for the lifetime of the bridge."""


class ReceiverService(ABC):
    """Posts StandardMessages into one connector."""

    connector_id: str
    supports_reactions: bool = False
    supports_emoji: bool = False
    supports_pins: bool = False
    supports_typing: bool = False
    supports_edits: bool = False

    @abstractmethod
    async def receive(self, message: StandardMessage, *, target_channel_id: str) -> list[str]:
        """Post `message` into `target_channel_id` on this connector, splitting
        it into multiple platform posts if it exceeds that platform's
        per-message length limit.

        Returns the native message ID of every post made, in order, for sync
        tracking. Raises `PartialRelayError` (rather than losing them) if some
        posts succeeded before a later one failed.
        """

    async def add_reaction(self, *, target_channel_id: str, target_message_id: str, emoji: str | CustomEmoji) -> None:
        """Add `emoji` to `target_message_id`. Only called when
        `supports_reactions`. Should be idempotent (no-op if the bridge has
        already reacted with `emoji`) so a second origin user reacting with
        the same emoji doesn't double-add."""
        raise NotImplementedError

    async def remove_reaction(
        self, *, target_channel_id: str, target_message_id: str, emoji: str | CustomEmoji
    ) -> None:
        """Remove the bridge's own `emoji` reaction from `target_message_id`.
        Only called when `supports_reactions`. Should be idempotent (no-op if
        the bridge isn't currently reacting with `emoji`)."""
        raise NotImplementedError

    async def edit_message(
        self, *, target_channel_id: str, target_message_ids: list[str], edit: StandardEdit
    ) -> None:
        """Re-render `edit.new_content_markdown` and edit the bridge's relayed
        copy of a message whose source was edited. `target_message_ids` is
        every native post the original relay produced in this channel, in order
        (a long message may have been split across several). Only called when
        `supports_edits`. Best-effort and silent — a post that's since been
        deleted, or one the platform won't let the bridge edit, is skipped."""
        raise NotImplementedError

    async def set_pinned(self, *, target_channel_id: str, target_message_id: str, pinned: bool) -> None:
        """Pin (`pinned=True`) or unpin (`pinned=False`) `target_message_id`.
        Idempotent — a no-op if the message is already in that state. Only
        called when `supports_pins`."""
        raise NotImplementedError

    async def trigger_typing(self, *, target_channel_id: str) -> None:
        """Show a "someone is typing" indicator in `target_channel_id` for a
        few seconds. Only called when `supports_typing`. Best-effort and
        safe to call repeatedly while the origin user keeps typing — the
        indicator naturally lapses once the calls stop. The indicator is
        always attributed to the bridge bot itself; no platform in this
        bridge can surface it under the originating user's name."""
        raise NotImplementedError

    async def stop_typing(self, *, target_channel_id: str) -> None:
        """Clear a typing indicator started via `trigger_typing`, in response
        to an explicit "stopped typing" event on the origin connector. Only
        called when `supports_typing`. Optional and best-effort: the default
        does nothing; each supporting receiver overrides it (Stoat ends the
        indicator now, Discord stops re-arming its keep-alive and lets its
        own ~10s timeout lapse — Discord has no clear-typing API)."""

    async def create_emoji(self, emoji: CustomEmoji) -> CustomEmoji | None:
        """Mirror `emoji` onto this connector, returning it with this connector's
        native ID — or None if it can't be created here (emoji slots full,
        name taken, invalid image, etc.), which callers treat as "skip this
        connector", not an error. Only called when `supports_emoji`."""
        raise NotImplementedError


class PartialRelayError(Exception):
    """Raised by `ReceiverService.receive()` when a message was split into
    multiple platform posts and only some of them succeeded before a later
    one raised — so the caller can still record what did get posted instead
    of treating the whole relay as if nothing happened."""

    def __init__(self, partial_ids: list[str], cause: BaseException) -> None:
        super().__init__(f"partial relay: {len(partial_ids)} post(s) delivered before failure: {cause}")
        self.partial_ids = partial_ids
        self.cause = cause


class UnsupportedRelayTargetError(Exception):
    """Raised by a `ReceiverService` when the resolved target channel is a
    kind this bridge simply can't post a relayed message into — e.g. a Discord
    forum/media channel, where every top-level post has to open a new thread
    (`thread_name`/`thread_id`), which the bridge has no sensible mapping for.
    Unlike a transient failure this will never succeed on retry, so
    `BridgeCoordinator` logs it once (no traceback) and drops the relay."""
