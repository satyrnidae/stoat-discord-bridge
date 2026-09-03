"""Wires each connector's sender service (listens, emits StandardMessages) to
every other connector's receiver service (posts into that platform), and
records what got relayed where via MongoDB for sync tracking.

Nothing links automatically: `ChannelMappingRepository` rows only come from
the `/link channel` and `/mirror channel` admin commands (see
admin_commands.py and each services/*.py module's command handler).
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING

from stoat_discord_bridge.admin_commands import (
    CategoryLinker,
    ChannelLinker,
    ConnectorInfo,
    EmoteLinker,
    RoleLinker,
    UserLinker,
)
from stoat_discord_bridge.config import BridgeConfig
from stoat_discord_bridge.health_server import start_health_server
from stoat_discord_bridge.models import (
    CustomEmoji,
    StandardEmojiCreated,
    StandardEmojiDeleted,
    StandardMessage,
    StandardPin,
    StandardReaction,
    StandardTyping,
)
from stoat_discord_bridge.services.base import PartialRelayError, ReceiverService
from stoat_discord_bridge.services.discord_service import (
    DiscordReceiverService,
    DiscordSenderService,
)
from stoat_discord_bridge.services.irc_service import (
    IrcReceiverService,
    IrcSenderService,
)
from stoat_discord_bridge.services.stoat_service import (
    StoatReceiverService,
    StoatSenderService,
)
from stoat_discord_bridge.status import HealthTracker
from stoat_discord_bridge.storage.category_mappings import CategoryMappingRepository, ThreadCategoryRepository
from stoat_discord_bridge.storage.channel_mappings import (
    ChannelMapping,
    ChannelMappingRepository,
)
from stoat_discord_bridge.storage.emoji_mappings import EmojiMappingRepository, EmojiRef
from stoat_discord_bridge.storage.message_sync import MessageRef, MessageSyncRepository
from stoat_discord_bridge.storage.mongo import MongoStore
from stoat_discord_bridge.storage.role_mappings import RoleMapping, RoleMappingRepository
from stoat_discord_bridge.storage.user_mappings import UserMappingRepository

if TYPE_CHECKING:
    from stoat_discord_bridge.services.role_sync import RolePermissionOverride

logger = logging.getLogger(__name__)

_PIN_SUPPRESS_TTL = 10.0


class BridgeCoordinator:
    """Routes an incoming StandardMessage to every other connector's channel
    mapped into the same bridge group, via that connector's ReceiverService."""

    def __init__(
        self,
        channel_mappings: ChannelMappingRepository,
        message_sync: MessageSyncRepository,
        emoji_mappings: EmojiMappingRepository,
        health: HealthTracker,
    ) -> None:
        self._receivers: dict[str, ReceiverService] = {}
        self._channel_mappings = channel_mappings
        self._message_sync = message_sync
        self._emoji_mappings = emoji_mappings
        self._health = health
        # A ~10s record of pin writes we just issued, keyed
        # (connector_id, channel_id, message_id, pinned), so the pin/unpin
        # event our own set_pinned() triggers is dropped rather than fanned
        # back out - same two-layer loop guard (idempotent hook + short-TTL
        # record) RoleSyncCoordinator uses.
        self._recent_pins: dict[tuple[str, str, str, bool], float] = {}

    def register_receiver(self, receiver: ReceiverService) -> None:
        """Every bridged connector's receiver must be registered before any
        sender's start() begins emitting messages into handle_incoming()."""
        self._receivers[receiver.connector_id] = receiver

    async def handle_incoming(self, message: StandardMessage) -> None:
        bridge_group = await self._channel_mappings.get_bridge_group(
            message.origin_connector_id, message.origin_channel_id
        )
        if bridge_group is None:
            return  # this channel isn't bridged

        targets = await self._channel_mappings.get_mapped_channels(bridge_group)
        per_target = await asyncio.gather(
            *(
                self._relay_to(message, target)
                for target in targets
                if target.connector_id != message.origin_connector_id
            )
        )
        relayed = [ref for refs in per_target for ref in refs]

        if relayed:
            origin = MessageRef(
                connector_id=message.origin_connector_id,
                channel_id=message.origin_channel_id,
                message_id=message.message_id,
            )
            await self._message_sync.record(bridge_group, origin, relayed)

    async def _relay_to(self, message: StandardMessage, target: ChannelMapping) -> list[MessageRef]:
        receiver = self._receivers.get(target.connector_id)
        if receiver is None:
            logger.warning("no receiver registered for %s, dropping relay", target.connector_id)
            return []
        try:
            native_ids = await receiver.receive(message, target_channel_id=target.channel_id)
        except PartialRelayError as exc:
            logger.warning(
                "relay from %s to %s partially failed: %s",
                message.origin_connector_id,
                target.connector_id,
                exc.cause,
                exc_info=exc.cause,
            )
            self._health.record_error(target.connector_id)
            native_ids = exc.partial_ids
        except Exception:
            logger.exception("relay from %s to %s failed", message.origin_connector_id, target.connector_id)
            self._health.record_error(target.connector_id)
            return []
        else:
            self._health.record_success(target.connector_id)
            logger.debug(
                "relayed message %s from %s to %s (channel %s)",
                message.message_id,
                message.origin_connector_id,
                target.connector_id,
                target.channel_id,
            )
        return [
            MessageRef(connector_id=target.connector_id, channel_id=target.channel_id, message_id=native_id)
            for native_id in native_ids
        ]

    async def handle_typing(self, typing: StandardTyping) -> None:
        """Relay a "someone is typing" / "stopped typing" indicator onto every
        other connector's channel mapped into the same bridge group.
        Fire-and-forget: nothing is recorded and no echo guard is needed - the
        bridge posts via webhook/masquerade, which don't themselves emit typing
        events, and each sender already drops typing from its own bot user. A
        `typing.active == False` event (an explicit stop, Stoat only) routes to
        `stop_typing` instead of `trigger_typing`. Silently does nothing if the
        origin channel isn't bridged or a target doesn't advertise
        `supports_typing` (IRC)."""
        bridge_group = await self._channel_mappings.get_bridge_group(
            typing.origin_connector_id, typing.origin_channel_id
        )
        if bridge_group is None:
            return  # this channel isn't bridged

        targets = await self._channel_mappings.get_mapped_channels(bridge_group)
        for target in targets:
            if target.connector_id == typing.origin_connector_id:
                continue
            receiver = self._receivers.get(target.connector_id)
            if receiver is None or not receiver.supports_typing:
                continue
            try:
                if typing.active:
                    await receiver.trigger_typing(target_channel_id=target.channel_id)
                else:
                    await receiver.stop_typing(target_channel_id=target.channel_id)
            except Exception:
                logger.exception(
                    "typing relay from %s to %s failed", typing.origin_connector_id, target.connector_id
                )

    async def handle_reaction(self, reaction: StandardReaction) -> None:
        """Relay a reaction add/remove onto every other connector's copy of
        the same message, translating custom emoji IDs along the way.
        Silently does nothing if the message was never bridged, or a given
        target never got a matching copy of the emoji - both are expected,
        not error conditions."""
        group = await self._message_sync.find_group(
            reaction.origin_connector_id, reaction.origin_channel_id, reaction.origin_message_id
        )
        if group is None:
            return  # this message isn't tracked as bridged

        if reaction.added and reaction.origin_reactor_count is not None and reaction.origin_reactor_count > 1:
            return  # someone already reacted with this on the origin - the bridge mirrored it then
        if not reaction.added and reaction.origin_reactor_count:
            return  # other origin users still hold this reaction - keep the mirror until the last one goes

        for ref in group:
            if ref.connector_id == reaction.origin_connector_id:
                continue
            receiver = self._receivers.get(ref.connector_id)
            if receiver is None or not receiver.supports_reactions:
                continue
            emoji = await self._translate_emoji(reaction.origin_connector_id, reaction.emoji, ref.connector_id)
            if emoji is None:
                continue  # custom emoji missing on this connector - ignore per spec
            try:
                if reaction.added:
                    await receiver.add_reaction(
                        target_channel_id=ref.channel_id, target_message_id=ref.message_id, emoji=emoji
                    )
                else:
                    await receiver.remove_reaction(
                        target_channel_id=ref.channel_id, target_message_id=ref.message_id, emoji=emoji
                    )
            except Exception:
                logger.exception("reaction relay from %s to %s failed", reaction.origin_connector_id, ref.connector_id)

    async def handle_pin(self, pin: StandardPin) -> None:
        """Relay a pin/unpin onto every other connector's copy of the same
        message. Silently does nothing if the message was never bridged, or a
        target connector doesn't advertise `supports_pins` (IRC) - both are
        expected, not errors. Loop-safe: a `set_pinned` we issued is recorded
        briefly so the resulting echo event is dropped here."""
        now = time.monotonic()
        self._recent_pins = {k: v for k, v in self._recent_pins.items() if now - v < _PIN_SUPPRESS_TTL}
        if (
            self._recent_pins.pop(
                (pin.origin_connector_id, pin.origin_channel_id, pin.origin_message_id, pin.pinned), None
            )
            is not None
        ):
            return  # our own write echoing back

        group = await self._message_sync.find_group(
            pin.origin_connector_id, pin.origin_channel_id, pin.origin_message_id
        )
        if group is None:
            return  # this message isn't tracked as bridged

        for ref in group:
            if ref.connector_id == pin.origin_connector_id:
                continue
            receiver = self._receivers.get(ref.connector_id)
            if receiver is None or not receiver.supports_pins:
                continue
            self._recent_pins[(ref.connector_id, ref.channel_id, ref.message_id, pin.pinned)] = time.monotonic()
            try:
                await receiver.set_pinned(
                    target_channel_id=ref.channel_id, target_message_id=ref.message_id, pinned=pin.pinned
                )
            except Exception:
                logger.exception("pin relay from %s to %s failed", pin.origin_connector_id, ref.connector_id)

    async def _translate_emoji(
        self, origin_connector_id: str, emoji: str | CustomEmoji, target_connector_id: str
    ) -> str | CustomEmoji | None:
        if isinstance(emoji, str):
            return emoji  # unicode emoji is universal, no translation needed
        ref = await self._emoji_mappings.find_equivalent_ref(
            origin_connector_id, emoji.native_id, target_connector_id
        )
        if ref is None:
            return None  # never mirrored to this connector (or mirroring failed) - caller should skip
        # Use the target ref's own stored name, not the origin emoji's: a
        # reaction event's emoji often carries no name (Stoat's `_parse_stoat_emoji`
        # leaves it blank), and a target that needs `name:id` (Discord) rejects
        # a blank name with "Unknown Emoji".
        return CustomEmoji(
            native_id=ref.emoji_id,
            name=ref.name or emoji.name,
            image_url=emoji.image_url,
            animated=emoji.animated,
        )

    async def handle_emoji_created(self, created: StandardEmojiCreated) -> None:
        """Mirror a newly created custom emoji onto every other connector
        that supports custom emoji, skipping (not failing) any connector that
        can't create it - full emoji slots, a rejected name, etc.

        Guarded to run at most once per (origin_connector_id, native_id) via
        an atomic reserve rather than a check-then-act exists()-then-record()
        pair: a sender's own self-echo filtering isn't fully trustworthy
        (e.g. Discord's is based on `emoji.user`, which the gateway payload
        doesn't reliably populate), and two such duplicate events arriving
        close together could otherwise both pass a plain existence check
        before either recorded its result, mirroring the same emoji twice."""
        origin = EmojiRef(
            connector_id=created.origin_connector_id, emoji_id=created.emoji.native_id, name=created.emoji.name
        )
        group_id = await self._emoji_mappings.try_reserve(origin)
        if group_id is None:
            return  # already known, or a concurrent call just won the race

        mirrored_refs: list[EmojiRef] = []
        for connector_id, receiver in self._receivers.items():
            if connector_id == created.origin_connector_id or not receiver.supports_emoji:
                continue
            try:
                mirrored = await receiver.create_emoji(created.emoji)
            except Exception:
                logger.exception(
                    "emoji mirror of %s from %s to %s failed",
                    created.emoji.name,
                    created.origin_connector_id,
                    connector_id,
                )
                continue
            if mirrored is None:
                continue  # this connector couldn't create it - skip per spec
            mirrored_refs.append(EmojiRef(connector_id=connector_id, emoji_id=mirrored.native_id, name=mirrored.name))

        if mirrored_refs:
            await self._emoji_mappings.add_refs(group_id, mirrored_refs)
        else:
            await self._emoji_mappings.release(group_id)

    async def handle_emoji_deleted(self, deleted: StandardEmojiDeleted) -> None:
        """A custom emoji was removed on one connector. Never mirror the
        deletion onto other connectors - a copy still in use elsewhere should
        keep working there. Just drop this connector's entry from the mapping
        group; EmojiMappingRepository.forget() only deletes the whole group
        once every connector's copy is gone."""
        await self._emoji_mappings.forget(deleted.origin_connector_id, deleted.native_id)


class RoleSyncCoordinator:
    """Keeps cross-connector-linked roles coherent after they're linked:

    - **auto-grant** (`handle`): a linked user gaining/losing a linked role
      on one connector has the linked role granted/revoked for their linked
      identity on every other connector.
    - **rename** (`handle_role_renamed`): a linked role renamed on one
      connector is renamed to match on every other connector's linked copy,
      and the stored `role_name` is refreshed.
    - **delete** (`handle_role_deleted`): a linked role deleted on one
      connector drops just that connector's mapping entry (the counterpart
      roles stay - they may still be in use); a group left with one member
      or none is dissolved.
    - **permissions** (`handle_channel_role_permission`): a linked role's
      permission override on a bridge-linked channel/category changing on
      one connector is mirrored onto the linked channel's copy for the
      linked role on every other connector - only the small subset of
      permission bits that mean the same thing on both platforms
      (services/role_sync.NEUTRAL_PERMISSIONS), spliced onto the target's
      existing override so unmapped bits are left alone.

    Best-effort and silent: an unlinked user/role, a target connector
    without the relevant hook, or a hook that raises are all skipped, not
    surfaced - matching the reaction/emoji sync stance.

    Loop prevention has two layers: (1) every hook is idempotent (no-op if
    already in the desired state), so the echo event this write triggers
    diffs to nothing; (2) a short-TTL record of writes just issued, so an
    echo that still carries a delta is dropped before it fans back out.
    """

    _SUPPRESS_TTL = 10.0

    def __init__(
        self,
        role_mappings: RoleMappingRepository,
        user_mappings: UserMappingRepository,
        connectors: dict[str, ConnectorInfo],
        channel_mappings: ChannelMappingRepository | None = None,
        category_mappings: CategoryMappingRepository | None = None,
    ) -> None:
        self._role_mappings = role_mappings
        self._user_mappings = user_mappings
        self._connectors = connectors
        self._channel_mappings = channel_mappings
        self._category_mappings = category_mappings
        self._recent: dict[tuple[str, str, str, bool], float] = {}
        self._recent_renames: dict[tuple[str, str, str], float] = {}
        self._recent_perms: dict[tuple[str, str, str], float] = {}

    async def handle_channel_role_permission(
        self,
        origin_connector_id: str,
        channel_id: str,
        role_id: str,
        override: "RolePermissionOverride",
        *,
        is_category: bool = False,
    ) -> None:
        """A linked role's permission override on a bridge-linked channel (or
        category, `is_category=True`) changed. Mirror the mapped bits onto
        every other connector's linked channel for the linked role. No-op if
        the channel/category or the role isn't linked to that connector, or
        the target already matches."""
        now = time.monotonic()
        self._recent_perms = {k: v for k, v in self._recent_perms.items() if now - v < self._SUPPRESS_TTL}
        if self._recent_perms.pop((origin_connector_id, channel_id, role_id), None) is not None:
            return  # our own write echoing back
        repo = self._category_mappings if is_category else self._channel_mappings
        if repo is None:
            return
        bridge_group = await repo.get_bridge_group(origin_connector_id, channel_id)
        if bridge_group is None:
            return
        members = (
            await repo.get_mapped_categories(bridge_group)
            if is_category
            else await repo.get_mapped_channels(bridge_group)
        )
        for m in members:
            if m.connector_id == origin_connector_id:
                continue
            info = self._connectors.get(m.connector_id)
            if info is None or info.set_channel_role_permission is None:
                continue
            target_channel_id = m.category_id if is_category else m.channel_id
            target_role_id = await self._role_mappings.find_linked_role_id(
                origin_connector_id, role_id, m.connector_id
            )
            if target_role_id is None:
                continue
            current = None
            if info.get_channel_role_permission is not None:
                try:
                    current = await info.get_channel_role_permission(target_channel_id, target_role_id)
                except Exception:
                    current = None
            spliced = override.splice_onto(current)
            if spliced == current:
                continue
            self._recent_perms[(m.connector_id, target_channel_id, target_role_id)] = time.monotonic()
            try:
                await info.set_channel_role_permission(target_channel_id, target_role_id, spliced)
            except Exception:
                logger.exception(
                    "[role-sync] mirroring perms for role %s on %s channel %s failed",
                    target_role_id,
                    m.connector_id,
                    target_channel_id,
                )
            else:
                logger.info(
                    "[role-sync] mirrored perms for role %s onto %s channel %s (from %s)",
                    target_role_id,
                    m.connector_id,
                    target_channel_id,
                    origin_connector_id,
                )

    async def handle_role_renamed(self, origin_connector_id: str, role_id: str, new_name: str) -> None:
        """A role was renamed on `origin_connector_id`. If it's linked,
        rename every other connector's linked copy to match and refresh the
        stored names. No-op if the role isn't linked or the stored name
        already matches everywhere (which is how the rename echo terminates)."""
        now = time.monotonic()
        self._recent_renames = {k: v for k, v in self._recent_renames.items() if now - v < self._SUPPRESS_TTL}
        if self._recent_renames.pop((origin_connector_id, role_id, new_name), None) is not None:
            return  # our own rename echoing back
        bridge_group = await self._role_mappings.get_bridge_group(origin_connector_id, role_id)
        if bridge_group is None:
            return
        mapped = await self._role_mappings.get_mapped_roles(bridge_group)
        for m in mapped:
            if m.role_name == new_name:
                continue
            await self._role_mappings.upsert(
                RoleMapping(
                    bridge_group=bridge_group,
                    connector_id=m.connector_id,
                    role_id=m.role_id,
                    role_name=new_name,
                )
            )
            if m.connector_id == origin_connector_id:
                continue
            info = self._connectors.get(m.connector_id)
            if info is None or info.rename_role is None:
                continue
            self._recent_renames[(m.connector_id, m.role_id, new_name)] = time.monotonic()
            try:
                await info.rename_role(m.role_id, new_name)
            except Exception:
                logger.exception("[role-sync] renaming role %s on %s failed", m.role_id, m.connector_id)
            else:
                logger.info("[role-sync] renamed role %s -> %r on %s (from %s)", m.role_id, new_name, m.connector_id, origin_connector_id)

    async def handle_role_deleted(self, origin_connector_id: str, role_id: str) -> None:
        """A role was deleted on `origin_connector_id`. Drop just its mapping
        entry; never touch the counterpart roles. Dissolve a group left with
        <= 1 member."""
        bridge_group = await self._role_mappings.get_bridge_group(origin_connector_id, role_id)
        if bridge_group is None:
            return
        mapped = await self._role_mappings.get_mapped_roles(bridge_group)
        await self._role_mappings.delete_mapping(origin_connector_id, role_id)
        survivors = [m for m in mapped if m.connector_id != origin_connector_id]
        if len(survivors) <= 1:
            for m in survivors:
                await self._role_mappings.delete_mapping(m.connector_id, m.role_id)
        logger.info("[role-sync] role %s deleted on %s - dropped from its bridge group", role_id, origin_connector_id)

    async def handle(
        self,
        origin_connector_id: str,
        user_id: str,
        added_role_ids: set[str],
        removed_role_ids: set[str],
    ) -> None:
        for role_id in set(added_role_ids):
            await self._propagate(origin_connector_id, user_id, role_id, added=True)
        for role_id in set(removed_role_ids):
            await self._propagate(origin_connector_id, user_id, role_id, added=False)

    async def _propagate(self, origin: str, user_id: str, role_id: str, *, added: bool) -> None:
        if self._consume_recent(origin, user_id, role_id, added):
            return  # this is our own write echoing back
        for target_id, info in self._connectors.items():
            if target_id == origin:
                continue
            hook = info.grant_role if added else info.revoke_role
            if hook is None:
                continue
            target_user_id = await self._user_mappings.find_linked_user_id(origin, user_id, target_id)
            if target_user_id is None:
                continue
            target_role_id = await self._role_mappings.find_linked_role_id(origin, role_id, target_id)
            if target_role_id is None:
                continue
            self._remember(target_id, target_user_id, target_role_id, added)
            try:
                await hook(target_user_id, target_role_id)
            except Exception:
                logger.exception(
                    "[role-grant] %s %s's role %s on %s failed",
                    "granting" if added else "revoking",
                    target_user_id,
                    target_role_id,
                    target_id,
                )
            else:
                logger.info(
                    "[role-grant] %s role %s %s %s on %s (from %s)",
                    target_role_id,
                    "->" if added else "<-",
                    target_user_id,
                    "granted" if added else "revoked",
                    target_id,
                    origin,
                )

    def _remember(self, connector_id: str, user_id: str, role_id: str, added: bool) -> None:
        self._recent[(connector_id, user_id, role_id, added)] = time.monotonic()

    def _consume_recent(self, connector_id: str, user_id: str, role_id: str, added: bool) -> bool:
        now = time.monotonic()
        self._recent = {k: v for k, v in self._recent.items() if now - v < self._SUPPRESS_TTL}
        return self._recent.pop((connector_id, user_id, role_id, added), None) is not None


async def run(config: BridgeConfig) -> None:
    mongo = MongoStore(config.mongo)
    channel_mappings = ChannelMappingRepository(mongo.db)
    message_sync = MessageSyncRepository(mongo.db)
    emoji_mappings = EmojiMappingRepository(mongo.db)
    await emoji_mappings.ensure_indexes()
    user_mappings = UserMappingRepository(mongo.db)
    category_mappings = CategoryMappingRepository(mongo.db)
    thread_categories = ThreadCategoryRepository(mongo.db)
    role_mappings = RoleMappingRepository(mongo.db)

    all_connectors = (*config.discord, *config.stoat, *config.irc)
    logger.info(
        "starting bridge with %d connector(s): %s",
        len(all_connectors),
        ", ".join(f"{c.id} ({c.label})" for c in all_connectors) or "none",
    )
    health = HealthTracker({c.id: c.label for c in all_connectors})

    coordinator = BridgeCoordinator(channel_mappings, message_sync, emoji_mappings, health)

    # Populated in place as each sender/receiver below is constructed;
    # ChannelLinker only reads this once a command fires (well after `run()`
    # finishes wiring), so construction order doesn't matter.
    connector_infos: dict[str, ConnectorInfo] = {}
    linker = ChannelLinker(channel_mappings, connector_infos, category_mappings)
    emote_linker = EmoteLinker(emoji_mappings, connector_infos)
    user_linker = UserLinker(user_mappings, connector_infos)
    category_linker = CategoryLinker(category_mappings, thread_categories, linker, connector_infos)
    role_linker = RoleLinker(role_mappings, connector_infos)
    role_grants = RoleSyncCoordinator(
        role_mappings, user_mappings, connector_infos, channel_mappings, category_mappings
    )

    senders: list = []
    closables: list = []

    for dc in config.discord:
        sender = DiscordSenderService(
            dc,
            on_message=coordinator.handle_incoming,
            health=health,
            on_reaction=coordinator.handle_reaction,
            on_emoji_created=coordinator.handle_emoji_created,
            on_emoji_deleted=coordinator.handle_emoji_deleted,
            on_pin=coordinator.handle_pin,
            on_typing=coordinator.handle_typing,
            linker=linker,
            emote_linker=emote_linker,
            user_linker=user_linker,
            category_linker=category_linker,
            role_linker=role_linker,
            on_member_roles_changed=role_grants.handle,
            on_role_renamed=role_grants.handle_role_renamed,
            on_role_deleted=role_grants.handle_role_deleted,
            on_channel_role_permission_changed=role_grants.handle_channel_role_permission,
        )
        receiver = DiscordReceiverService(
            client=sender.client,
            guild_id=dc.guild_id,
            connector_id=dc.id,
            user_mappings=user_mappings,
            enable_local_user_masquerade=dc.enable_local_user_masquerade,
            channel_mappings=channel_mappings,
            role_mappings=role_mappings,
            emoji_mappings=emoji_mappings,
        )
        coordinator.register_receiver(receiver)
        connector_infos[dc.id] = ConnectorInfo(
            id=dc.id,
            label=dc.label,
            resolve_channel_name=sender.get_channel_name,
            resolve_channel_id_by_name=sender.resolve_channel_id_by_name,
            resolve_channel_category=sender.get_channel_category,
            describe_channel=sender.describe_channel,
            ensure_channel=sender.ensure_channel,
            can_view_channel=sender.can_view_channel,
            resolve_user_name=sender.get_user_name,
            resolve_user_id_by_name=sender.resolve_user_id_by_name,
            resolve_category_name=sender.get_category_name,
            resolve_category_id_by_name=sender.resolve_category_id_by_name,
            ensure_category=sender.ensure_category,
            channels_in_category=sender.channels_in_category,
            move_channel_to_category=sender.move_channel_to_category,
            resolve_role_name=sender.get_role_name,
            resolve_role_id_by_name=sender.resolve_role_id_by_name,
            ensure_role=sender.ensure_role,
            grant_role=sender.grant_role,
            revoke_role=sender.revoke_role,
            rename_role=sender.rename_role,
            get_channel_role_permission=sender.get_channel_role_permission,
            set_channel_role_permission=sender.set_channel_role_permission,
            resolve_emoji_name=sender.get_emoji_name,
            resolve_emoji_id_by_name=sender.resolve_emoji_id_by_name,
            resolve_emoji=sender.resolve_emoji,
            ensure_emoji=receiver.create_emoji,
            list_channels=sender.list_channels,
            list_categories=sender.list_categories,
            list_roles=sender.list_roles,
            list_users=sender.list_users,
            list_emotes=sender.list_emotes,
        )
        senders.append(sender)
        closables.extend([receiver, sender])

    for sc in config.stoat:
        sender = StoatSenderService(
            sc,
            on_message=coordinator.handle_incoming,
            health=health,
            on_reaction=coordinator.handle_reaction,
            on_emoji_created=coordinator.handle_emoji_created,
            on_emoji_deleted=coordinator.handle_emoji_deleted,
            on_pin=coordinator.handle_pin,
            on_typing=coordinator.handle_typing,
            linker=linker,
            emote_linker=emote_linker,
            user_linker=user_linker,
            category_linker=category_linker,
            role_linker=role_linker,
            on_member_roles_changed=role_grants.handle,
            on_role_renamed=role_grants.handle_role_renamed,
            on_role_deleted=role_grants.handle_role_deleted,
            on_channel_role_permission_changed=role_grants.handle_channel_role_permission,
        )
        receiver = StoatReceiverService(
            sender,
            user_mappings=user_mappings,
            channel_mappings=channel_mappings,
            role_mappings=role_mappings,
            emoji_mappings=emoji_mappings,
        )
        coordinator.register_receiver(receiver)
        connector_infos[sc.id] = ConnectorInfo(
            id=sc.id,
            label=sc.label,
            resolve_channel_name=sender.get_channel_name,
            resolve_channel_id_by_name=sender.resolve_channel_id_by_name,
            resolve_channel_category=sender.get_channel_category,
            describe_channel=sender.describe_channel,
            can_view_channel=sender.can_view_channel,
            ensure_channel=sender.ensure_channel,
            resolve_user_name=sender.get_user_name,
            resolve_user_id_by_name=sender.resolve_user_id_by_name,
            resolve_category_name=sender.get_category_name,
            resolve_category_id_by_name=sender.resolve_category_id_by_name,
            ensure_category=sender.ensure_category,
            channels_in_category=sender.channels_in_category,
            move_channel_to_category=sender.move_channel_to_category,
            resolve_role_name=sender.get_role_name,
            resolve_role_id_by_name=sender.resolve_role_id_by_name,
            ensure_role=sender.ensure_role,
            grant_role=sender.grant_role,
            revoke_role=sender.revoke_role,
            rename_role=sender.rename_role,
            get_channel_role_permission=sender.get_channel_role_permission,
            set_channel_role_permission=sender.set_channel_role_permission,
            resolve_emoji_name=sender.get_emoji_name,
            resolve_emoji_id_by_name=sender.resolve_emoji_id_by_name,
            resolve_emoji=sender.resolve_emoji,
            ensure_emoji=receiver.create_emoji,
            list_channels=sender.list_channels,
            list_categories=sender.list_categories,
            list_roles=sender.list_roles,
            list_users=sender.list_users,
            list_emotes=sender.list_emotes,
        )
        senders.append(sender)
        closables.append(sender)

    for ic in config.irc:
        boot_channels = [m.channel_id for m in await channel_mappings.get_all_for_connector(ic.id)]
        sender = IrcSenderService(
            ic,
            boot_channels,
            on_message=coordinator.handle_incoming,
            health=health,
            linker=linker,
            user_linker=user_linker,
        )
        coordinator.register_receiver(
            IrcReceiverService(
                sender,
                user_mappings=user_mappings,
                enable_local_user_masquerade=ic.enable_local_user_masquerade,
                channel_mappings=channel_mappings,
                role_mappings=role_mappings,
                emoji_mappings=emoji_mappings,
            )
        )
        connector_infos[ic.id] = ConnectorInfo(
            id=ic.id,
            label=ic.label,
            on_channel_linked=sender.join_channel,
            on_channel_unlinked=sender.part_channel,
            ensure_channel=sender.ensure_channel,
            resolve_channel_id_by_name=sender.resolve_channel_id_by_name,
            list_channels=sender.list_channels,
        )
        senders.append(sender)
        closables.append(sender)

    health_runner = await start_health_server(health)

    try:
        await asyncio.gather(*(sender.start() for sender in senders))
    finally:
        logger.info("shutting down bridge")
        await asyncio.gather(*(closable.close() for closable in closables), return_exceptions=True)
        await health_runner.cleanup()
        mongo.close()
