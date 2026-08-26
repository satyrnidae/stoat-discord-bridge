"""Wires each connector's sender service (listens, emits StandardMessages) to
every other connector's receiver service (posts into that platform), and
records what got relayed where via MongoDB for sync tracking.

Nothing links automatically: `ChannelMappingRepository` rows only come from
the `/link-channel` and `/mirror-channels` admin commands (see
admin_commands.py and each services/*.py module's command handler).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

from stoat_discord_bridge.admin_commands import ChannelLinker, ConnectorInfo, EmoteLinker, StructureMirrorer, UserLinker
from stoat_discord_bridge.channel_structure import GuildStructure
from stoat_discord_bridge.config import BridgeConfig
from stoat_discord_bridge.health_server import start_health_server
from stoat_discord_bridge.models import (
    CustomEmoji,
    StandardEmojiCreated,
    StandardEmojiDeleted,
    StandardMessage,
    StandardReaction,
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
from stoat_discord_bridge.storage.channel_mappings import (
    ChannelMapping,
    ChannelMappingRepository,
)
from stoat_discord_bridge.storage.emoji_mappings import EmojiMappingRepository, EmojiRef
from stoat_discord_bridge.storage.message_sync import MessageRef, MessageSyncRepository
from stoat_discord_bridge.storage.mongo import MongoStore
from stoat_discord_bridge.storage.user_mappings import UserMappingRepository

logger = logging.getLogger(__name__)


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

    async def _translate_emoji(
        self, origin_connector_id: str, emoji: str | CustomEmoji, target_connector_id: str
    ) -> str | CustomEmoji | None:
        if isinstance(emoji, str):
            return emoji  # unicode emoji is universal, no translation needed
        native_id = await self._emoji_mappings.find_equivalent(origin_connector_id, emoji.native_id, target_connector_id)
        if native_id is None:
            return None  # never mirrored to this connector (or mirroring failed) - caller should skip
        return CustomEmoji(native_id=native_id, name=emoji.name, image_url=emoji.image_url, animated=emoji.animated)

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


async def run(config: BridgeConfig) -> None:
    mongo = MongoStore(config.mongo)
    channel_mappings = ChannelMappingRepository(mongo.db)
    message_sync = MessageSyncRepository(mongo.db)
    emoji_mappings = EmojiMappingRepository(mongo.db)
    await emoji_mappings.ensure_indexes()
    user_mappings = UserMappingRepository(mongo.db)

    all_connectors = (*config.discord, *config.stoat, *config.irc)
    logger.info(
        "starting bridge with %d connector(s): %s",
        len(all_connectors),
        ", ".join(f"{c.id} ({c.label})" for c in all_connectors) or "none",
    )
    health = HealthTracker({c.id: c.label for c in all_connectors})

    coordinator = BridgeCoordinator(channel_mappings, message_sync, emoji_mappings, health)

    # Populated in place as each sender/receiver below is constructed;
    # ChannelLinker/StructureMirrorer only read these once a command fires
    # (well after `run()` finishes wiring), so construction order doesn't
    # matter.
    connector_infos: dict[str, ConnectorInfo] = {}
    structure_providers: dict[str, Callable[[], GuildStructure]] = {}
    linker = ChannelLinker(channel_mappings, connector_infos)
    mirrorer = StructureMirrorer(structure_providers)
    emote_linker = EmoteLinker(emoji_mappings, connector_infos)
    user_linker = UserLinker(user_mappings, connector_infos)

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
            linker=linker,
            emote_linker=emote_linker,
            user_linker=user_linker,
        )
        structure_providers[dc.id] = sender.snapshot_guild_structure
        receiver = DiscordReceiverService(
            client=sender.client, guild_id=dc.guild_id, connector_id=dc.id, user_mappings=user_mappings
        )
        coordinator.register_receiver(receiver)
        # No ensure_channel: Discord has no channel-creation capability in
        # this codebase, so /mirror-channel reports it unsupported rather
        # than this hook ever being called.
        connector_infos[dc.id] = ConnectorInfo(
            id=dc.id, label=dc.label, resolve_channel_name=sender.get_channel_name, resolve_user_name=sender.get_user_name
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
            linker=linker,
            mirrorer=mirrorer,
            emote_linker=emote_linker,
            user_linker=user_linker,
        )
        coordinator.register_receiver(StoatReceiverService(sender, user_mappings=user_mappings))
        connector_infos[sc.id] = ConnectorInfo(
            id=sc.id,
            label=sc.label,
            resolve_channel_name=sender.get_channel_name,
            ensure_channel=sender.ensure_channel,
            resolve_user_name=sender.get_user_name,
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
            emote_linker=emote_linker,
            user_linker=user_linker,
        )
        coordinator.register_receiver(IrcReceiverService(sender, user_mappings=user_mappings))
        connector_infos[ic.id] = ConnectorInfo(
            id=ic.id, label=ic.label, on_channel_linked=sender.join_channel, ensure_channel=sender.ensure_channel
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
