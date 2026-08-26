"""Shared logic behind the `/link-channel` and `/mirror-channels` admin
commands, called identically from each connector's own command handler
(services/discord_service.py, stoat_service.py, irc_service.py) so the
bridge-group/conflict logic isn't duplicated three times.

Channels never link automatically - a bridge_group only comes into being via
`ChannelLinker.link_channel`, called directly by `/link-channel` or, per
channel created/matched, by the Stoat `/mirror-channels` handler.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from stoat_discord_bridge.channel_structure import GuildStructure
from stoat_discord_bridge.storage.channel_mappings import ChannelMapping, ChannelMappingRepository
from stoat_discord_bridge.storage.emoji_mappings import EmojiMappingRepository, EmojiRef
from stoat_discord_bridge.storage.user_mappings import UserMapping, UserMappingRepository


class LinkError(Exception):
    """User-facing error - callers should relay str(exc) back to the admin who ran the command."""


@dataclass(frozen=True)
class ConnectorInfo:
    id: str
    label: str
    # Best-effort native-channel-id -> display-name lookup for the *other*
    # side of a link (the side that isn't "the channel the command was run
    # in", whose name we don't otherwise know). None, an exception, or a
    # falsy return all fall back to using the raw id as the name.
    resolve_channel_name: Callable[[str], Awaitable[str | None]] | None = None
    # Called with a freshly-linked channel id on this connector. Only IRC
    # connectors set this, to JOIN the channel immediately instead of
    # waiting for a restart to pick up the new mapping.
    on_channel_linked: Callable[[str], Awaitable[None]] | None = None
    # Idempotent get-or-create: ensures a channel named `name` exists on
    # this connector, returning its native id (existing or newly created).
    # None if this connector kind doesn't support channel creation (e.g.
    # Discord has no channel-creation capability in this codebase at all -
    # /mirror-channel then reports that connector as unsupported rather than
    # calling this).
    ensure_channel: Callable[[str], Awaitable[str]] | None = None


class ChannelLinker:
    def __init__(self, channel_mappings: ChannelMappingRepository, connectors: dict[str, ConnectorInfo]) -> None:
        # `connectors` is populated in place by bridge.run() as each sender/
        # receiver is constructed - read lazily here, only once a command
        # actually fires, so construction order doesn't matter.
        self._channel_mappings = channel_mappings
        self._connectors = connectors

    async def link_channel(
        self,
        *,
        local_connector: str,
        local_channel_id: str,
        local_channel_name: str,
        source: str,
        source_id: str,
        destination_id: str | None,
    ) -> str:
        """Link `source`'s `source_id` channel to `destination_id` (or the
        invoking channel, if omitted) on `local_connector`. Returns a
        human-readable summary. Raises LinkError if `source` is unknown, the
        two channels are the same channel, or both are already linked to two
        *different* existing bridge groups (no auto-merge - the operator has
        to unlink one side first)."""
        if source not in self._connectors:
            raise LinkError(f"'{source}' isn't a known connector.")

        if destination_id is None or destination_id == local_channel_id:
            destination_channel_id = local_channel_id
            destination_name = local_channel_name
        else:
            destination_channel_id = destination_id
            destination_name = await self._resolve_name(local_connector, destination_id)

        if source == local_connector and source_id == destination_channel_id:
            raise LinkError("can't link a channel to itself.")

        source_group = await self._channel_mappings.get_bridge_group(source, source_id)
        destination_group = await self._channel_mappings.get_bridge_group(local_connector, destination_channel_id)
        if source_group and destination_group and source_group != destination_group:
            raise LinkError(
                "both channels are already linked, but to different bridge groups - unlink one before relinking."
            )
        bridge_group = source_group or destination_group or uuid.uuid4().hex

        source_name = await self._resolve_name(source, source_id)
        await self._channel_mappings.upsert(
            ChannelMapping(bridge_group=bridge_group, connector_id=source, channel_id=source_id, channel_name=source_name)
        )
        await self._channel_mappings.upsert(
            ChannelMapping(
                bridge_group=bridge_group,
                connector_id=local_connector,
                channel_id=destination_channel_id,
                channel_name=destination_name,
            )
        )
        await self._notify_linked(source, source_id)
        await self._notify_linked(local_connector, destination_channel_id)

        source_label = self._connectors[source].label
        local_info = self._connectors.get(local_connector)
        local_label = local_info.label if local_info else local_connector
        return (
            f"Linked {source_label} channel '{source_name}' ({source_id}) to "
            f"{local_label} channel '{destination_name}' ({destination_channel_id})."
        )

    async def mirror_channel(
        self, *, local_connector: str, local_channel_id: str, local_channel_name: str, destination: str
    ) -> str:
        """Ensure `local_channel_id` (on `local_connector`) has a linked
        counterpart on `destination`: reuses an existing same-name channel
        there if `destination`'s ensure_channel() finds/creates one and
        `link_channel` doesn't hit a group conflict, skips outright if
        already synced there, and reports (rather than raises for) a
        destination that can't create channels or a link conflict - the
        caller (a bulk "mirror to every connector" loop) shouldn't have one
        bad destination abort the rest."""
        if destination not in self._connectors:
            raise LinkError(f"'{destination}' isn't a known connector.")
        if destination == local_connector:
            raise LinkError("can't mirror a channel to its own connector.")

        bridge_group = await self._channel_mappings.get_bridge_group(local_connector, local_channel_id)
        if bridge_group is not None:
            existing = await self._channel_mappings.get_mapped_channels(bridge_group)
            if any(m.connector_id == destination for m in existing):
                return f"{self._connectors[destination].label}: already synced - skipped."

        dest_info = self._connectors[destination]
        if dest_info.ensure_channel is None:
            return f"{dest_info.label}: doesn't support channel creation - link it manually with /link-channel."

        try:
            destination_channel_id = await dest_info.ensure_channel(local_channel_name)
        except Exception as exc:
            return f"{dest_info.label}: failed to create/find a channel: {exc}"

        try:
            return await self.link_channel(
                local_connector=destination,
                local_channel_id=destination_channel_id,
                local_channel_name=local_channel_name,
                source=local_connector,
                source_id=local_channel_id,
                destination_id=None,
            )
        except LinkError as exc:
            return f"{dest_info.label}: {exc}"

    async def mirror_channel_all(
        self, *, local_connector: str, local_channel_id: str, local_channel_name: str
    ) -> str:
        """`/mirror-channel all` - mirror_channel() against every other
        configured connector, one line of summary/skip/error per connector
        rather than stopping at the first problem."""
        results = [
            await self.mirror_channel(
                local_connector=local_connector,
                local_channel_id=local_channel_id,
                local_channel_name=local_channel_name,
                destination=destination,
            )
            for destination in self._connectors
            if destination != local_connector
        ]
        return "\n".join(results) if results else "no other connectors configured."

    async def list_linked_channels(self, *, local_connector: str, local_channel_id: str) -> str:
        """Human-readable listing of every channel bridged to
        `local_channel_id` on `local_connector` (the invoking channel),
        across every connector in its bridge group - for the
        `/linked-channels` command. Read-only, so unlike `link_channel` it
        never raises LinkError; an unlinked channel just gets a plain
        "nothing here" reply."""
        bridge_group = await self._channel_mappings.get_bridge_group(local_connector, local_channel_id)
        if bridge_group is None:
            return "This channel isn't linked to any others."

        mapped = await self._channel_mappings.get_mapped_channels(bridge_group)
        lines = []
        for mapping in sorted(mapped, key=lambda m: (m.connector_id, m.channel_id)):
            info = self._connectors.get(mapping.connector_id)
            label = info.label if info else mapping.connector_id
            marker = (
                " (this channel)"
                if mapping.connector_id == local_connector and mapping.channel_id == local_channel_id
                else ""
            )
            lines.append(f"{label}: {mapping.channel_name} ({mapping.channel_id}){marker}")
        return "Linked channels:\n" + "\n".join(lines)

    async def _resolve_name(self, connector_id: str, channel_id: str) -> str:
        info = self._connectors.get(connector_id)
        if info is None or info.resolve_channel_name is None:
            return channel_id
        try:
            name = await info.resolve_channel_name(channel_id)
        except Exception:
            return channel_id
        return name or channel_id

    async def _notify_linked(self, connector_id: str, channel_id: str) -> None:
        info = self._connectors.get(connector_id)
        if info is None or info.on_channel_linked is None:
            return
        await info.on_channel_linked(channel_id)


class EmoteLinker:
    def __init__(self, emoji_mappings: EmojiMappingRepository, connectors: dict[str, ConnectorInfo]) -> None:
        self._emoji_mappings = emoji_mappings
        self._connectors = connectors

    async def link_emote(self, *, local_connector: str, local_id: str, source: str, source_id: str) -> str:
        """Link `source`'s `source_id` emoji to `local_id` on `local_connector`.
        Raises LinkError if `source` is unknown, the two are the same emoji,
        or both already belong to two *different* existing mapping groups."""
        if source not in self._connectors:
            raise LinkError(f"'{source}' isn't a known connector.")
        if source == local_connector and source_id == local_id:
            raise LinkError("can't link an emote to itself.")

        source_group = await self._emoji_mappings.get_group_id(source, source_id)
        local_group = await self._emoji_mappings.get_group_id(local_connector, local_id)
        if source_group and local_group and source_group != local_group:
            raise LinkError(
                "both emotes are already linked, but to different mapping groups - unlink one before relinking."
            )

        if source_group is None and local_group is None:
            group_id = await self._emoji_mappings.try_reserve(EmojiRef(connector_id=source, emoji_id=source_id, name=source_id))
            if group_id is None:
                # lost a race to a concurrent reservation - fall back to whatever group now owns it
                group_id = await self._emoji_mappings.get_group_id(source, source_id)
            await self._emoji_mappings.add_refs(group_id, [EmojiRef(connector_id=local_connector, emoji_id=local_id, name=local_id)])
        elif local_group is None:
            await self._emoji_mappings.add_refs(source_group, [EmojiRef(connector_id=local_connector, emoji_id=local_id, name=local_id)])
        elif source_group is None:
            await self._emoji_mappings.add_refs(local_group, [EmojiRef(connector_id=source, emoji_id=source_id, name=source_id)])
        # else: source_group == local_group already - no-op, already linked

        source_label = self._connectors[source].label
        local_info = self._connectors.get(local_connector)
        local_label = local_info.label if local_info else local_connector
        return f"Linked {source_label} emote '{source_id}' to {local_label} emote '{local_id}'."


class UserLinker:
    def __init__(self, user_mappings: UserMappingRepository, connectors: dict[str, ConnectorInfo]) -> None:
        self._user_mappings = user_mappings
        self._connectors = connectors

    async def link_user(self, *, local_connector: str, local_user_id: str, source: str, source_user_id: str) -> str:
        """Link `source`'s `source_user_id` to `local_user_id` on `local_connector`.
        Raises LinkError if `source` is unknown, the two are already the same
        identity, or both already belong to two *different* existing link groups."""
        if source not in self._connectors:
            raise LinkError(f"'{source}' isn't a known connector.")
        if source == local_connector and source_user_id == local_user_id:
            raise LinkError("can't link a user to themselves.")

        source_group = await self._user_mappings.get_link_group(source, source_user_id)
        local_group = await self._user_mappings.get_link_group(local_connector, local_user_id)
        if source_group and local_group and source_group != local_group:
            raise LinkError(
                "both users are already linked, but to different link groups - unlink one before relinking."
            )
        link_group = source_group or local_group or uuid.uuid4().hex

        await self._user_mappings.upsert(
            UserMapping(link_group=link_group, connector_id=source, user_id=source_user_id, display_name=source_user_id)
        )
        await self._user_mappings.upsert(
            UserMapping(link_group=link_group, connector_id=local_connector, user_id=local_user_id, display_name=local_user_id)
        )

        source_label = self._connectors[source].label
        local_info = self._connectors.get(local_connector)
        local_label = local_info.label if local_info else local_connector
        return f"Linked {source_label} user '{source_user_id}' to {local_label} user '{local_user_id}'."


class StructureMirrorer:
    """Looks up which configured connector can produce a GuildStructure
    snapshot for a given `<source>` id, for the Stoat `/mirror-channels`
    command. Only Discord connectors register a provider today."""

    def __init__(self, structure_providers: dict[str, Callable[[], GuildStructure]]) -> None:
        self._structure_providers = structure_providers

    def get_structure(self, source: str) -> GuildStructure:
        provider = self._structure_providers.get(source)
        if provider is None:
            raise LinkError(f"'{source}' isn't a known structure source (must be a Discord connector).")
        return provider()
