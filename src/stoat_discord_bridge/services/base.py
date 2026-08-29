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
    StandardEmojiCreated,
    StandardEmojiDeleted,
    StandardMessage,
    StandardReaction,
)

OnMessage = Callable[[StandardMessage], Awaitable[None]]
OnReaction = Callable[[StandardReaction], Awaitable[None]]
OnEmojiCreated = Callable[[StandardEmojiCreated], Awaitable[None]]
OnEmojiDeleted = Callable[[StandardEmojiDeleted], Awaitable[None]]
# (origin_connector_id, user_id, added_role_ids, removed_role_ids) - a linked
# user's role set changed on one connector; see bridge.py's RoleSyncCoordinator.
OnMemberRolesChanged = Callable[[str, str, set[str], set[str]], Awaitable[None]]
# (origin_connector_id, role_id, new_name) - a role was renamed on one connector.
OnRoleRenamed = Callable[[str, str, str], Awaitable[None]]
# (origin_connector_id, role_id) - a role was deleted on one connector.
OnRoleDeleted = Callable[[str, str], Awaitable[None]]


class SenderService(ABC):
    """Listens to one connector and emits StandardMessages for the bridge to relay."""

    connector_id: str

    def __init__(
        self,
        on_message: OnMessage,
        on_reaction: OnReaction | None = None,
        on_emoji_created: OnEmojiCreated | None = None,
        on_emoji_deleted: OnEmojiDeleted | None = None,
    ) -> None:
        self._on_message = on_message
        self._on_reaction = on_reaction
        self._on_emoji_created = on_emoji_created
        self._on_emoji_deleted = on_emoji_deleted

    @abstractmethod
    async def start(self) -> None:
        """Connect and begin listening. Runs for the lifetime of the bridge."""


class ReceiverService(ABC):
    """Posts StandardMessages into one connector."""

    connector_id: str
    supports_reactions: bool = False
    supports_emoji: bool = False

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
        """Add `emoji` to `target_message_id`. Only called when `supports_reactions`."""
        raise NotImplementedError

    async def remove_reaction(
        self, *, target_channel_id: str, target_message_id: str, emoji: str | CustomEmoji
    ) -> None:
        """Remove the bridge's own `emoji` reaction from `target_message_id`. Only called when `supports_reactions`."""
        raise NotImplementedError

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
