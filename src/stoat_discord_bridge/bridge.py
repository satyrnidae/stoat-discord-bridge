"""Wires each endpoint's sender service (listens, emits StandardMessages) to
every other endpoint's receiver service (posts into that platform), and
records what got relayed where via MongoDB for sync tracking.

Discord -> Stoat channel/category *structure* mirroring is available as the
Stoat `/mirror-channels` admin command (see services/stoat_service.py and
channel_structure.py). It only creates matching channels/categories; it
does not wire up bridging between them — channel mappings are still read
from Mongo and must be seeded by hand via ChannelMappingRepository.upsert().
"""

from __future__ import annotations

import asyncio

from stoat_discord_bridge.config import BridgeConfig
from stoat_discord_bridge.models import (
    CustomEmoji,
    Platform,
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


class BridgeCoordinator:
    """Routes an incoming StandardMessage to every other platform's channel
    mapped into the same bridge group, via that platform's ReceiverService."""

    def __init__(
        self,
        channel_mappings: ChannelMappingRepository,
        message_sync: MessageSyncRepository,
        emoji_mappings: EmojiMappingRepository,
        health: HealthTracker,
    ) -> None:
        self._receivers: dict[Platform, ReceiverService] = {}
        self._channel_mappings = channel_mappings
        self._message_sync = message_sync
        self._emoji_mappings = emoji_mappings
        self._health = health

    def register_receiver(self, receiver: ReceiverService) -> None:
        """Every bridged platform's receiver must be registered before any
        sender's start() begins emitting messages into handle_incoming()."""
        self._receivers[receiver.platform] = receiver

    async def handle_incoming(self, message: StandardMessage) -> None:
        bridge_group = await self._channel_mappings.get_bridge_group(
            message.origin_platform, message.origin_channel_id
        )
        if bridge_group is None:
            return  # this channel isn't bridged

        targets = await self._channel_mappings.get_mapped_channels(bridge_group)
        per_target = await asyncio.gather(
            *(
                self._relay_to(message, target)
                for target in targets
                if target.platform != message.origin_platform
            )
        )
        relayed = [ref for refs in per_target for ref in refs]

        if relayed:
            origin = MessageRef(
                platform=message.origin_platform,
                channel_id=message.origin_channel_id,
                message_id=message.message_id,
            )
            await self._message_sync.record(bridge_group, origin, relayed)

    async def _relay_to(self, message: StandardMessage, target: ChannelMapping) -> list[MessageRef]:
        receiver = self._receivers.get(target.platform)
        if receiver is None:
            print(f"[bridge] no receiver registered for {target.platform.value}, dropping relay")
            return []
        try:
            native_ids = await receiver.receive(message, target_channel_id=target.channel_id)
        except PartialRelayError as exc:
            print(f"[bridge] relay to {target.platform.value} partially failed: {exc.cause}")
            self._health.record_error(target.platform)
            native_ids = exc.partial_ids
        except Exception as exc:
            print(f"[bridge] relay to {target.platform.value} failed: {exc}")
            self._health.record_error(target.platform)
            return []
        else:
            self._health.record_success(target.platform)
        return [
            MessageRef(platform=target.platform, channel_id=target.channel_id, message_id=native_id)
            for native_id in native_ids
        ]

    async def handle_reaction(self, reaction: StandardReaction) -> None:
        """Relay a reaction add/remove onto every other platform's copy of
        the same message, translating custom emoji IDs along the way.
        Silently does nothing if the message was never bridged, or a given
        target never got a matching copy of the emoji - both are expected,
        not error conditions."""
        group = await self._message_sync.find_group(
            reaction.origin_platform, reaction.origin_channel_id, reaction.origin_message_id
        )
        if group is None:
            return  # this message isn't tracked as bridged

        for ref in group:
            if ref.platform == reaction.origin_platform:
                continue
            receiver = self._receivers.get(ref.platform)
            if receiver is None or not receiver.supports_reactions:
                continue
            emoji = await self._translate_emoji(reaction.origin_platform, reaction.emoji, ref.platform)
            if emoji is None:
                continue  # custom emoji missing on this platform - ignore per spec
            try:
                if reaction.added:
                    await receiver.add_reaction(
                        target_channel_id=ref.channel_id, target_message_id=ref.message_id, emoji=emoji
                    )
                else:
                    await receiver.remove_reaction(
                        target_channel_id=ref.channel_id, target_message_id=ref.message_id, emoji=emoji
                    )
            except Exception as exc:
                print(f"[bridge] reaction relay to {ref.platform.value} failed: {exc}")

    async def _translate_emoji(
        self, origin_platform: Platform, emoji: str | CustomEmoji, target_platform: Platform
    ) -> str | CustomEmoji | None:
        if isinstance(emoji, str):
            return emoji  # unicode emoji is universal, no translation needed
        native_id = await self._emoji_mappings.find_equivalent(origin_platform, emoji.native_id, target_platform)
        if native_id is None:
            return None  # never mirrored to this platform (or mirroring failed) - caller should skip
        return CustomEmoji(native_id=native_id, name=emoji.name, image_url=emoji.image_url, animated=emoji.animated)

    async def handle_emoji_created(self, created: StandardEmojiCreated) -> None:
        """Mirror a newly created custom emoji onto every other platform
        that supports custom emoji, skipping (not failing) any platform that
        can't create it - full emoji slots, a rejected name, etc.

        Guarded to run at most once per (origin_platform, native_id) via an
        atomic reserve rather than a check-then-act exists()-then-record()
        pair: a sender's own self-echo filtering isn't fully trustworthy
        (e.g. Discord's is based on `emoji.user`, which the gateway payload
        doesn't reliably populate), and two such duplicate events arriving
        close together could otherwise both pass a plain existence check
        before either recorded its result, mirroring the same emoji twice."""
        origin = EmojiRef(platform=created.origin_platform, emoji_id=created.emoji.native_id, name=created.emoji.name)
        group_id = await self._emoji_mappings.try_reserve(origin)
        if group_id is None:
            return  # already known, or a concurrent call just won the race

        mirrored_refs: list[EmojiRef] = []
        for platform, receiver in self._receivers.items():
            if platform == created.origin_platform or not receiver.supports_emoji:
                continue
            try:
                mirrored = await receiver.create_emoji(created.emoji)
            except Exception as exc:
                print(f"[bridge] emoji mirror to {platform.value} failed: {exc}")
                continue
            if mirrored is None:
                continue  # this platform couldn't create it - skip per spec
            mirrored_refs.append(EmojiRef(platform=platform, emoji_id=mirrored.native_id, name=mirrored.name))

        if mirrored_refs:
            await self._emoji_mappings.add_refs(group_id, mirrored_refs)
        else:
            await self._emoji_mappings.release(group_id)

    async def handle_emoji_deleted(self, deleted: StandardEmojiDeleted) -> None:
        """A custom emoji was removed on one platform. Never mirror the
        deletion onto other platforms - a copy still in use elsewhere should
        keep working there. Just drop this platform's entry from the mapping
        group; EmojiMappingRepository.forget() only deletes the whole group
        once every platform's copy is gone."""
        await self._emoji_mappings.forget(deleted.origin_platform, deleted.native_id)


async def run(config: BridgeConfig) -> None:
    mongo = MongoStore(config.mongo)
    channel_mappings = ChannelMappingRepository(mongo.db)
    message_sync = MessageSyncRepository(mongo.db)
    emoji_mappings = EmojiMappingRepository(mongo.db)
    await emoji_mappings.ensure_indexes()
    health = HealthTracker(list(Platform))

    coordinator = BridgeCoordinator(channel_mappings, message_sync, emoji_mappings, health)

    discord_sender = DiscordSenderService(
        config.discord,
        on_message=coordinator.handle_incoming,
        health=health,
        on_reaction=coordinator.handle_reaction,
        on_emoji_created=coordinator.handle_emoji_created,
        on_emoji_deleted=coordinator.handle_emoji_deleted,
    )
    stoat_public_sender = StoatSenderService(
        config.stoat_public,
        Platform.STOAT_PUBLIC,
        on_message=coordinator.handle_incoming,
        health=health,
        on_reaction=coordinator.handle_reaction,
        on_emoji_created=coordinator.handle_emoji_created,
        on_emoji_deleted=coordinator.handle_emoji_deleted,
        guild_structure_provider=discord_sender.snapshot_guild_structure,
    )
    stoat_selfhosted_sender = StoatSenderService(
        config.stoat_selfhosted,
        Platform.STOAT_SELFHOSTED,
        on_message=coordinator.handle_incoming,
        health=health,
        on_reaction=coordinator.handle_reaction,
        on_emoji_created=coordinator.handle_emoji_created,
        on_emoji_deleted=coordinator.handle_emoji_deleted,
        guild_structure_provider=discord_sender.snapshot_guild_structure,
    )
    irc_channels = [m.channel_id for m in await channel_mappings.get_all_for_platform(Platform.IRC)]
    irc_sender = IrcSenderService(config.irc, irc_channels, on_message=coordinator.handle_incoming, health=health)

    discord_receiver = DiscordReceiverService(client=discord_sender, guild_id=config.discord.guild_id)
    coordinator.register_receiver(discord_receiver)
    coordinator.register_receiver(StoatReceiverService(stoat_public_sender))
    coordinator.register_receiver(StoatReceiverService(stoat_selfhosted_sender))
    coordinator.register_receiver(IrcReceiverService(irc_sender))

    try:
        await asyncio.gather(
            discord_sender.start(),
            stoat_public_sender.start(),
            stoat_selfhosted_sender.start(),
            irc_sender.start(),
        )
    finally:
        await discord_receiver.close()
        await asyncio.gather(
            discord_sender.close(),
            stoat_public_sender.close(),
            stoat_selfhosted_sender.close(),
            irc_sender.close(),
            return_exceptions=True,
        )
        mongo.close()
