"""Shared logic behind the `/link-channel` and `/mirror-channels` admin
commands, called identically from each connector's own command handler
(services/discord_service.py, stoat_service.py, irc_service.py) so the
bridge-group/conflict logic isn't duplicated three times.

Channels never link automatically - a bridge_group only comes into being via
`ChannelLinker.link_channel`, called directly by `/link-channel` or, per
channel created/matched, by the Stoat `/mirror-channels` handler.
"""

from __future__ import annotations

import logging
import re
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from stoat_discord_bridge.channel_structure import GuildStructure
from stoat_discord_bridge.storage.channel_mappings import ChannelMapping, ChannelMappingRepository
from stoat_discord_bridge.storage.emoji_mappings import EmojiMappingRepository, EmojiRef
from stoat_discord_bridge.storage.user_mappings import UserMapping, UserMappingRepository

logger = logging.getLogger(__name__)

# Only Discord's `/link-user` uses a real member-picker (see
# discord_service.py's _handle_link_user); Stoat's and IRC's equivalents take
# the id as free text, and a Discord id typed/pasted there commonly comes in
# as a full `<@id>`/`<@!id>` mention (e.g. copied straight out of Discord)
# rather than the bare snowflake - which then never matches a real Discord
# user and shows up unresolved (as the literal mention) in /linked-users.
# No native id on any other connector kind looks like this, so stripping it
# unconditionally here is safe regardless of which connector is involved.
_DISCORD_MENTION_RE = re.compile(r"^<@!?(\d+)>$")


def _strip_discord_mention(raw: str) -> str:
    match = _DISCORD_MENTION_RE.match(raw.strip())
    return match.group(1) if match else raw


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
    # The second argument is an optional Category name - if given, the
    # matched-or-created channel should end up inside a same-named Category
    # on this connector (creating it if needed). None if this connector kind
    # doesn't support channel creation (e.g. Discord has no channel-creation
    # capability in this codebase at all - /mirror-channel then reports that
    # connector as unsupported rather than calling this).
    ensure_channel: Callable[[str, str | None], Awaitable[str]] | None = None
    # Best-effort native-user-id -> display-name lookup, for `/linked-users`
    # to show real names instead of raw ids. None, an exception, or a falsy
    # return all fall back to the raw id, same as resolve_channel_name.
    # IRC leaves this unset - a user_id there already IS the nick (see
    # storage/user_mappings.py's UserMapping.display_name docstring), so
    # there's nothing further to resolve.
    resolve_user_name: Callable[[str], Awaitable[str | None]] | None = None


class ChannelLinker:
    def __init__(self, channel_mappings: ChannelMappingRepository, connectors: dict[str, ConnectorInfo]) -> None:
        # `connectors` is populated in place by bridge.run() as each sender/
        # receiver is constructed - read lazily here, only once a command
        # actually fires, so construction order doesn't matter.
        self._channel_mappings = channel_mappings
        self._connectors = connectors

    @property
    def connectors(self) -> dict[str, ConnectorInfo]:
        """Every connector this bridge knows about, for admin-command UIs
        (e.g. Discord slash-command autocomplete on a `source`/`destination`
        option) to list without duplicating bridge.run()'s wiring."""
        return self._connectors

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
        self,
        *,
        local_connector: str,
        local_channel_id: str,
        local_channel_name: str,
        destination: str,
        local_channel_category: str | None = None,
    ) -> str:
        """Ensure `local_channel_id` (on `local_connector`) has a linked
        counterpart on `destination`: reuses an existing same-name channel
        there if `destination`'s ensure_channel() finds/creates one and
        `link_channel` doesn't hit a group conflict, skips outright if
        already synced there, and reports (rather than raises for) a
        destination that can't create channels or a link conflict - the
        caller (a bulk "mirror to every connector" loop) shouldn't have one
        bad destination abort the rest. `local_channel_category`, if given,
        is the Category the source channel belongs to on `local_connector` -
        `destination`'s ensure_channel() places the mirrored channel into a
        same-named Category there too."""
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
            destination_channel_id = await dest_info.ensure_channel(local_channel_name, local_channel_category)
        except Exception as exc:
            logger.warning("mirror-channel: %s.ensure_channel(%r) failed: %s", destination, local_channel_name, exc)
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
        self,
        *,
        local_connector: str,
        local_channel_id: str,
        local_channel_name: str,
        local_channel_category: str | None = None,
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
                local_channel_category=local_channel_category,
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

    async def unlink_channel(self, *, local_connector: str, local_channel_id: str, destination: str | None) -> str:
        """`/unlink-channel`. `destination` (a connector id) kicks just that
        one member out of `local_channel_id`'s bridge group - everyone else
        (including this channel) stays linked to each other; None/"all"
        (the default) dissolves the whole group instead, unlinking every
        member. Raises LinkError if the channel isn't linked, or
        `destination` isn't actually a member of its group."""
        bridge_group = await self._channel_mappings.get_bridge_group(local_connector, local_channel_id)
        if bridge_group is None:
            raise LinkError("this channel isn't linked to anything.")

        if destination is None or destination.lower() == "all":
            count = await self._channel_mappings.delete_bridge_group(bridge_group)
            return f"Unlinked this channel's entire bridge group ({count} channel(s) removed)."

        mapped = await self._channel_mappings.get_mapped_channels(bridge_group)
        target = next((m for m in mapped if m.connector_id == destination), None)
        if target is None:
            raise LinkError(f"'{destination}' isn't linked in this channel's bridge group.")
        await self._channel_mappings.delete_mapping(destination, target.channel_id)
        label = self._connectors[destination].label if destination in self._connectors else destination
        return f"Unlinked {label} channel '{target.channel_name}' ({target.channel_id}) from this bridge group."

    async def is_linked(self, connector_id: str, channel_id: str) -> bool:
        """Whether `channel_id` (on `connector_id`) already belongs to a
        bridge group - used by Discord's thread-creation auto-mirror (see
        DiscordSenderService._handle_thread_create) to gate on the thread's
        parent channel already being bridged, rather than mirroring every
        thread created anywhere in the guild."""
        return await self._channel_mappings.get_bridge_group(connector_id, channel_id) is not None

    async def _resolve_name(self, connector_id: str, channel_id: str) -> str:
        info = self._connectors.get(connector_id)
        if info is None or info.resolve_channel_name is None:
            return channel_id
        try:
            name = await info.resolve_channel_name(channel_id)
        except Exception:
            logger.debug("couldn't resolve channel name for %s on %s", channel_id, connector_id, exc_info=True)
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

    @property
    def connectors(self) -> dict[str, ConnectorInfo]:
        return self._connectors

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

    @property
    def connectors(self) -> dict[str, ConnectorInfo]:
        return self._connectors

    async def link_user(self, *, local_connector: str, local_user_id: str, source: str, source_user_id: str) -> str:
        """Link `source`'s `source_user_id` to `local_user_id` on `local_connector`.
        Raises LinkError if `source` is unknown, the two are already the same
        identity, or both already belong to two *different* existing link groups."""
        if source not in self._connectors:
            raise LinkError(f"'{source}' isn't a known connector.")
        source_user_id = _strip_discord_mention(source_user_id)
        local_user_id = _strip_discord_mention(local_user_id)
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

    async def list_linked_users(self, *, local_connector: str | None = None, local_user_id: str | None = None) -> str:
        """Human-readable listing of cross-connector user links, for the
        `/linked-users` debugging command. With no target given, lists every
        link group; given a specific (local_connector, local_user_id), shows
        just that identity's group. Real display names are resolved live
        from each connector (via ConnectorInfo.resolve_user_name) rather
        than read off the stored mapping, since that's just the id it was
        linked with, never a real name (see UserMapping.display_name)."""
        if local_connector is not None and local_user_id is not None:
            link_group = await self._user_mappings.get_link_group(local_connector, local_user_id)
            if link_group is None:
                return "This user isn't linked to any others."
            groups = [await self._user_mappings.get_mapped_users(link_group)]
        else:
            groups_by_id: dict[str, list[UserMapping]] = {}
            for mapping in await self._user_mappings.get_all():
                groups_by_id.setdefault(mapping.link_group, []).append(mapping)
            if not groups_by_id:
                return "No users are linked yet."
            groups = list(groups_by_id.values())

        lines = []
        for group_mappings in groups:
            parts = []
            for mapping in sorted(group_mappings, key=lambda m: (m.connector_id, m.user_id)):
                info = self._connectors.get(mapping.connector_id)
                label = info.label if info else mapping.connector_id
                name = await self._resolve_user_name(mapping.connector_id, mapping.user_id)
                # Only show the raw id alongside the name when it adds
                # information - for IRC (whose user_id already IS the nick)
                # or a failed/unconfigured resolution, they're identical.
                parts.append(f"{label}: {name}" if name == mapping.user_id else f"{label}: {name} ({mapping.user_id})")
            lines.append(" ↔ ".join(parts))
        return "Linked users:\n" + "\n".join(lines)

    async def unlink_user(self, *, local_connector: str, local_user_id: str, destination: str | None) -> str:
        """`/unlink-user`. `destination` (a connector id) kicks just that one
        identity out of `local_user_id`'s link group - everyone else
        (including this identity) stays linked to each other; None/"all"
        (the default) dissolves the whole group instead, unlinking every
        identity. Raises LinkError if the user isn't linked, or
        `destination` isn't actually a member of its group."""
        local_user_id = _strip_discord_mention(local_user_id)
        link_group = await self._user_mappings.get_link_group(local_connector, local_user_id)
        if link_group is None:
            raise LinkError("this user isn't linked to anything.")

        if destination is None or destination.lower() == "all":
            count = await self._user_mappings.delete_link_group(link_group)
            return f"Unlinked this user's entire link group ({count} identity/identities removed)."

        mapped = await self._user_mappings.get_mapped_users(link_group)
        target = next((m for m in mapped if m.connector_id == destination), None)
        if target is None:
            raise LinkError(f"'{destination}' isn't linked in this user's link group.")
        await self._user_mappings.delete_mapping(destination, target.user_id)
        label = self._connectors[destination].label if destination in self._connectors else destination
        return f"Unlinked {label} user '{target.user_id}' from this user's link group."

    async def _resolve_user_name(self, connector_id: str, user_id: str) -> str:
        info = self._connectors.get(connector_id)
        if info is None or info.resolve_user_name is None:
            return user_id
        try:
            name = await info.resolve_user_name(user_id)
        except Exception:
            logger.debug("couldn't resolve user name for %s on %s", user_id, connector_id, exc_info=True)
            return user_id
        return name or user_id


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
