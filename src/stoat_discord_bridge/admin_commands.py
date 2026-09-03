"""Shared logic behind the `/link channel` and `/link category` admin
commands, called identically from each connector's own command handler
(services/discord_service.py, stoat_service.py, irc_service.py) so the
bridge-group/conflict logic isn't duplicated three times.

Channels never link automatically - a bridge_group only comes into being via
`ChannelLinker.link_channel`, called directly by `/link channel` or `/mirror
channel`. Categories are the same - only `/link-category` creates a
CategoryLinker bridge_group -
but once a Category *is* linked, a new channel appearing inside it on either
side auto-syncs onto the other's linked Category (CategoryLinker.
sync_new_channel), which is the one place in this module something
auto-links without an explicit admin command.
"""

from __future__ import annotations

import logging
import re
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from stoat_discord_bridge.storage.category_mappings import (
    CategoryMapping,
    CategoryMappingRepository,
    ThreadCategoryRepository,
)
from stoat_discord_bridge.storage.channel_mappings import ChannelMapping, ChannelMappingRepository
from stoat_discord_bridge.storage.emoji_mappings import EmojiMappingRepository, EmojiRef
from stoat_discord_bridge.storage.role_mappings import RoleMapping, RoleMappingRepository
from stoat_discord_bridge.storage.user_mappings import UserMapping, UserMappingRepository

if TYPE_CHECKING:
    from stoat_discord_bridge.models import ChannelMetadata, CustomEmoji
    from stoat_discord_bridge.services.role_sync import RolePermissionOverride

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


# Emote command args commonly come in as an emoji token rather than a bare
# name/id: a `:shortcode:` (Discord/Stoat autocomplete, IRC habit) or a full
# Discord `<:name:id>` / `<a:name:id>` custom-emoji reference (pasted from a
# message). Reduce either to the bare name (or id) the resolve hooks expect.
_CUSTOM_EMOJI_RE = re.compile(r"^<a?:(\w+):(\w+)>$")
_EMOJI_SHORTCODE_RE = re.compile(r"^:([\w~+-]+):$")

# A token shaped like a native entity id rather than a human-chosen name: an
# all-digit Discord snowflake, or a 26-char Crockford-base32 ULID (Stoat). Used
# to decide whether an unresolvable `/mirror channel category:` value is a typo'd
# id (reject) or a name for a Category to create (pass through) - see
# ChannelLinker._resolve_destination_category_name.
_BARE_ID_RE = re.compile(r"\A(?:\d{15,}|[0-9A-HJKMNP-TV-Za-hjkmnp-tv-z]{26})\Z")


def _strip_emote_token(raw: str) -> str:
    token = raw.strip()
    match = _CUSTOM_EMOJI_RE.match(token)
    if match:
        return match.group(2)
    match = _EMOJI_SHORTCODE_RE.match(token)
    if match:
        return match.group(1)
    return token


def pop_kv_option(tokens: list[str], key: str) -> tuple[list[str], str | None]:
    """Pull the first ``key:value`` (or ``key=value``) token out of ``tokens``,
    returning ``(remaining tokens, value)`` - ``value`` is ``None`` when no such
    token is present. The key match is case-insensitive; the value keeps its
    original case. Discord models `/mirror channel`'s ``category`` option
    natively, but Stoat's `stoat.ext.commands` and IRC's bare DM commands parse
    positionally - a `category` that can hold arbitrary ids/names can't be
    positional there, so both take it as this `PARAM:value` pair anywhere in the
    argument list (issue #75).

    A quoted value (``key:"two words"`` / ``key:'two words'``) reassembles the
    tokens the caller's whitespace split broke it into, up to the closing quote,
    and the surrounding quotes are stripped - the only way a multi-word value
    survives, since both callers hand this a pre-split token list."""
    prefix_colon = f"{key.lower()}:"
    prefix_eq = f"{key.lower()}="
    value: str | None = None
    remaining: list[str] = []
    i = 0
    while i < len(tokens):
        token = tokens[i]
        lowered = token.lower()
        if value is None and (lowered.startswith(prefix_colon) or lowered.startswith(prefix_eq)):
            raw = token[len(key) + 1 :]
            quote = raw[:1]
            if quote in ('"', "'"):
                raw = raw[1:]
                while not raw.endswith(quote) and i + 1 < len(tokens):
                    i += 1
                    raw = f"{raw} {tokens[i]}"
                raw = raw[:-1] if raw.endswith(quote) else raw
            value = raw
        else:
            remaining.append(token)
        i += 1
    return remaining, value


def _clean_new_name(raw: str | None) -> str | None:
    """A `new_name` override off a `/mirror <noun> to|from` command, trimmed -
    or None if it was blank/absent, meaning "carry the source name over" (the
    historical behaviour). Never normalised here: each connector's `ensure_*`
    hook destination-normalises whatever name it's handed (IRC's `#channel`
    sterilising, Stoat's 32-char clip, an emoji-name reject, ...), so routing
    the override through that hook is what makes it "destination-normalised",
    and the same call still get-or-creates so a same-named existing entity is
    matched rather than duplicated (issue #44)."""
    if raw is None:
        return None
    stripped = raw.strip()
    return stripped or None


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
    # Best-effort native-channel-NAME -> id lookup, so the channel commands
    # (`/link channel` / `/mirror channel` / `/unlink channel` / `/linked
    # channels`) accept a bare channel name anywhere an id is expected.
    # None/exception/falsy return all mean "treat the token as an id already"
    # (ChannelLinker._resolve_to_id) - the same contract as
    # resolve_role_id_by_name. IRC wires this too (issue #41): a channel id
    # there is already `#name`, but the hook still sterilizes a bare token
    # into that shape (adds the `#`, drops characters IRC channel names
    # can't hold - see irc_service.formatting.normalize_channel_name), so
    # `/link channel irc general` behaves like `/link channel irc #general`.
    resolve_channel_id_by_name: Callable[[str], Awaitable[str | None]] | None = None
    # Fold a channel *name* into the shape this connector actually stores it
    # under, so a name carried over from another connector by `/mirror channel`
    # is recorded consistently with the id `ensure_channel` returned. Only IRC
    # wires this (to `normalize_channel_name`): a channel mirrored there as
    # `danksquad` gets id `#danksquad` from `ensure_channel`, and this keeps the
    # stored name `#danksquad` too rather than the bare `danksquad` (issue #51).
    # Synchronous; None (every other connector) leaves the name untouched.
    normalize_channel_name: Callable[[str], str] | None = None
    # Best-effort native-channel-id -> (category_id, category_name) lookup for
    # the Category a channel sits in, or None if it's uncategorised / can't be
    # resolved. Used by `/mirror channel from <service> <external_id>` to place
    # the freshly-created local channel into the local counterpart of the
    # source channel's linked Category (rather than a fresh same-named one).
    # IRC leaves this unset - it has no Category concept.
    resolve_channel_category: Callable[[str], Awaitable[tuple[str, str] | None]] | None = None
    # Best-effort "can the bridge bot actually see this channel?" check, keyed
    # by native channel id. Returns True (visible), False (the channel
    # resolves but the bot lacks the view permission on it), or None ("can't
    # tell" - bad id, uncached, an error, or a connector kind with no
    # visibility concept). `ChannelLinker.mirror_channel` refuses on an
    # explicit False so `/mirror channel` (both `to` and `from`) never mirrors
    # a channel the bot can't see into a stub named after the platform's
    # hidden-channel placeholder (issue #33). IRC leaves this unset - every
    # channel it's in is one it can see.
    can_view_channel: Callable[[str], Awaitable[bool | None]] | None = None
    # Called with a freshly-linked channel id on this connector. Only IRC
    # connectors set this, to JOIN the channel immediately instead of
    # waiting for a restart to pick up the new mapping.
    on_channel_linked: Callable[[str], Awaitable[None]] | None = None
    # Called (channel_id, unlinked_from) when a channel on this connector has
    # just lost its last linked counterpart - via /unlink channel on any
    # connector, whether the whole group was dissolved or a kick stranded
    # this channel alone. `unlinked_from` is a human-readable list of the
    # channels it was bridged to. Only IRC connectors set this, to post a
    # notice and PART - it's no longer bridged, so there's no reason to sit
    # in it. Discord/Stoat leave the channel in place (it's a real,
    # human-created channel there, not one the bridge necessarily made).
    on_channel_unlinked: Callable[[str, str], Awaitable[None]] | None = None
    # Best-effort read of a channel's cosmetic metadata (description, NSFW /
    # maturity flag, icon URL) as a `ChannelMetadata`, or None if the channel
    # can't be resolved. `/mirror channel` reads this off the *source*
    # channel and hands it to the destination's `ensure_channel` so a
    # freshly-mirrored channel isn't left blank (issue #32). IRC leaves it
    # unset - IRC channels carry none of those.
    describe_channel: Callable[[str], Awaitable["ChannelMetadata | None"]] | None = None
    # Idempotent get-or-create: ensures a channel named `name` exists on
    # this connector, returning its native id (existing or newly created).
    # The second argument is an optional Category name - if given, the
    # matched-or-created channel should end up inside a same-named Category
    # on this connector (creating it if needed). None if this connector kind
    # doesn't support channel creation (/mirror channel then reports that
    # connector as unsupported rather than calling this - only IRC leaves it
    # unset now that Discord and Stoat both implement it).
    # The third argument, is_thread_category, marks the matched/created
    # Category (if any) as one Discord's thread/forum-post auto-mirroring
    # created (see DiscordSenderService._handle_thread_create), via
    # CategoryLinker.bind_thread_category - so `/link-category` later
    # refuses to link it. False for every other caller (regular
    # /mirror channel, and CategoryLinker.sync_new_channel's own auto-sync).
    # The fourth argument, category_parent_channel_id, is this connector's
    # own channel id for the thread's parent channel (only set from the
    # thread auto-mirror). It keys the persistent parent->thread-Category
    # binding (ThreadCategoryRepository), so the Category is resolved by id
    # rather than by title on later threads - surviving a Category rename.
    # None for every other caller.
    # An optional `metadata` keyword (a `ChannelMetadata`) is passed by
    # `/mirror channel` when the source channel had any - the hook applies
    # it only when it actually creates the channel, never onto a reused one.
    ensure_channel: Callable[..., Awaitable[str]] | None = None
    # Best-effort native-user-id -> display-name lookup, for `/linked-users`
    # to show real names instead of raw ids. None, an exception, or a falsy
    # return all fall back to the raw id, same as resolve_channel_name.
    # IRC leaves this unset - a user_id there already IS the nick (see
    # storage/user_mappings.py's UserMapping.display_name docstring), so
    # there's nothing further to resolve.
    resolve_user_name: Callable[[str], Awaitable[str | None]] | None = None
    # Best-effort native-user-NAME -> id lookup (display name / username /
    # nickname), so `/link user` / `/unlink user` / `/linked users` accept a
    # bare name anywhere an id is expected. None/exception/falsy return all
    # mean "treat the token as an id already" (UserLinker._resolve_to_id).
    # IRC leaves this unset - a user_id there already IS the nick, so there's
    # nothing to resolve.
    resolve_user_id_by_name: Callable[[str], Awaitable[str | None]] | None = None
    # Best-effort native-category-id -> title lookup, the Category
    # counterpart of resolve_channel_name, used by CategoryLinker for
    # `/link category`. None on IRC (no Category concept there).
    resolve_category_name: Callable[[str], Awaitable[str | None]] | None = None
    # Best-effort native-category-NAME -> id lookup, so `/link category` /
    # `/mirror category` / `/unlink category` / `/linked categories` accept a
    # bare Category name anywhere an id is expected. None/exception/falsy
    # return all mean "treat the token as an id already"
    # (CategoryLinker._resolve_to_id). None on IRC.
    resolve_category_id_by_name: Callable[[str], Awaitable[str | None]] | None = None
    # Idempotent get-or-create by name: ensures a Category named `name` exists
    # on this connector, returning its native id (existing or newly created).
    # None if this connector kind can't create Categories - `/mirror category`
    # then reports that connector as unsupported (mirrors ensure_role). None
    # on IRC.
    ensure_category: Callable[[str], Awaitable[str]] | None = None
    # native-category-id -> [(channel_id, channel_name), ...] for every channel
    # inside that Category, used by `/mirror category` to enumerate the source
    # Category's channels. None on IRC.
    channels_in_category: Callable[[str], Awaitable[list[tuple[str, str]]]] | None = None
    # Move channel `channel_id` into Category `category_id` on this connector.
    # Idempotent (no-op if already there). Used by `/mirror category` to
    # relocate already-linked destination channels into the mirrored Category.
    # None on IRC.
    move_channel_to_category: Callable[[str, str], Awaitable[None]] | None = None
    # --- Role hooks (Discord/Stoat only; None everywhere on IRC, which has
    # no role concept). See RoleLinker below and bridge.py's
    # RoleSyncCoordinator. ---
    # Best-effort native-role-id -> name lookup, the role counterpart of
    # resolve_channel_name (same None/exception/falsy -> raw-id fallback).
    resolve_role_name: Callable[[str], Awaitable[str | None]] | None = None
    # Best-effort native-role-NAME -> id lookup, so `/link role` / `/mirror
    # role` / `/unlink role` accept a bare role name anywhere an id is
    # expected. None/exception/falsy return all mean "treat the token as an
    # id already" (RoleLinker._resolve_to_id).
    resolve_role_id_by_name: Callable[[str], Awaitable[str | None]] | None = None
    # Idempotent get-or-create by name: ensures a role named `name` exists on
    # this connector, returning its native id (existing or newly created).
    # None if this connector kind can't create roles - `/mirror role` then
    # reports that connector as unsupported rather than calling this (mirrors
    # ensure_channel).
    ensure_role: Callable[[str], Awaitable[str]] | None = None
    # Rename the role `role_id` to `new_name` on this connector - used to keep
    # linked copies coherent when a linked role is renamed on one side.
    rename_role: Callable[[str, str], Awaitable[None]] | None = None
    # Grant / revoke role `role_id` for user `user_id` on this connector.
    # Both are idempotent (no-op if already in the desired state) so the
    # bridge's own write doesn't echo back into an auto-grant loop.
    grant_role: Callable[[str, str], Awaitable[None]] | None = None
    revoke_role: Callable[[str, str], Awaitable[None]] | None = None
    # Read / write the permission override for role `role_id` on channel
    # `channel_id`, as a neutral RolePermissionOverride
    # (services/role_sync.py). The setter is idempotent. Used by the
    # per-channel permission-mirror flow.
    get_channel_role_permission: (
        Callable[[str, str], Awaitable["RolePermissionOverride | None"]] | None
    ) = None
    set_channel_role_permission: (
        Callable[[str, str, "RolePermissionOverride"], Awaitable[None]] | None
    ) = None
    # --- Emoji hooks (Discord/Stoat only; None everywhere on IRC, which has
    # no custom-emoji concept). See EmoteLinker below. ---
    # Best-effort native-emoji-id -> name lookup, the emoji counterpart of
    # resolve_role_name (same None/exception/falsy -> raw-id fallback).
    resolve_emoji_name: Callable[[str], Awaitable[str | None]] | None = None
    # Best-effort native-emoji-NAME -> id lookup, so `/link emote` / `/mirror
    # emote` / `/unlink emote` accept a bare emoji name anywhere an id is
    # expected. None/exception/falsy return all mean "treat the token as an id
    # already" (EmoteLinker._resolve_to_id).
    resolve_emoji_id_by_name: Callable[[str], Awaitable[str | None]] | None = None
    # native-emoji-id -> full CustomEmoji (name, image_url, animated) for the
    # source side of `/mirror emote`, which needs the image to recreate the
    # emoji elsewhere (unlike `/mirror role`, an emoji can't be created
    # name-only). None/missing emoji -> `/mirror emote` reports it can't read
    # the source.
    resolve_emoji: Callable[[str], Awaitable["CustomEmoji | None"]] | None = None
    # Create a copy of `emoji` on this connector, returning the created
    # CustomEmoji (with its new native id) or None if it couldn't - full
    # emoji slots, rejected name, image too large, etc. None (the hook
    # itself) if this connector kind can't create emoji - `/mirror emote`
    # then reports that connector as unsupported. Wired straight to the
    # receiver's existing create_emoji.
    ensure_emoji: Callable[["CustomEmoji"], Awaitable["CustomEmoji | None"]] | None = None
    # --- Autocomplete listing hooks. Each returns every entity of that kind
    # currently visible on this connector as [(native_id, display_name), ...]
    # (unsorted; the caller filters and caps). Discord's slash commands call
    # these to populate the `external_id` / `local_id` option autocomplete -
    # `external_id` off the connector picked in the `service` option,
    # `local_id` off the Discord connector itself - so an operator picks a
    # real channel/role/user/Category/emoji from the menu instead of pasting
    # a raw id. Best-effort: None (unset), an exception, or an empty list all
    # just mean "no suggestions" - the option still accepts a hand-typed id
    # or name. IRC wires only list_channels (issue #41), from the channels it
    # already knows - config plus anything linked; its other ids are already
    # human-typable names with nothing to enumerate. list_roles /
    # list_categories / list_emotes are Discord/Stoat only, like the rest of
    # their hook families. ---
    list_channels: Callable[[], Awaitable[list[tuple[str, str]]]] | None = None
    list_categories: Callable[[], Awaitable[list[tuple[str, str]]]] | None = None
    list_roles: Callable[[], Awaitable[list[tuple[str, str]]]] | None = None
    list_users: Callable[[], Awaitable[list[tuple[str, str]]]] | None = None
    list_emotes: Callable[[], Awaitable[list[tuple[str, str]]]] | None = None

    # --- Capability flags, derived from which hooks are wired ---------------
    # Whether this connector kind has the concept a given command family
    # operates on. IRC has none of roles/Categories/custom emoji, so it wires
    # none of those hooks - `/link role`, `/link category`, `/link emote` and
    # their siblings can't meaningfully target it, and Discord's slash-command
    # `service` autocomplete filters it out of those subcommands (see
    # services/discord_service/commands.py). Channels and users exist on every
    # connector kind, so there's no `supports_channels` / `supports_users`.
    @property
    def supports_roles(self) -> bool:
        return self.resolve_role_name is not None

    @property
    def supports_categories(self) -> bool:
        return self.resolve_category_name is not None

    @property
    def supports_emotes(self) -> bool:
        return self.resolve_emoji_name is not None


class ChannelLinker:
    def __init__(
        self,
        channel_mappings: ChannelMappingRepository,
        connectors: dict[str, ConnectorInfo],
        category_mappings: CategoryMappingRepository | None = None,
    ) -> None:
        # `connectors` is populated in place by bridge.run() as each sender/
        # receiver is constructed - read lazily here, only once a command
        # actually fires, so construction order doesn't matter.
        self._channel_mappings = channel_mappings
        self._connectors = connectors
        # Only `mirror_channel_from` reads this - to resolve the source
        # channel's Category to its already-linked local counterpart. None in
        # tests that don't exercise that path.
        self._category_mappings = category_mappings

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

        source_id = await self._resolve_to_id(source, source_id)

        if not destination_id or destination_id == local_channel_id:
            destination_channel_id = local_channel_id
            destination_name = local_channel_name
        else:
            destination_channel_id = await self._resolve_to_id(local_connector, destination_id)
            destination_name = await self._resolve_name(local_connector, destination_channel_id)

        if source == local_connector and source_id == destination_channel_id:
            raise LinkError("can't link a channel to itself.")

        source_group = await self._channel_mappings.get_bridge_group(source, source_id)
        destination_group = await self._channel_mappings.get_bridge_group(local_connector, destination_channel_id)
        if source_group and destination_group and source_group != destination_group:
            raise LinkError(
                "both channels are already linked, but to different bridge groups - unlink one before relinking."
            )
        bridge_group = source_group or destination_group or uuid.uuid4().hex

        source_name = self._normalize_name(source, await self._resolve_name(source, source_id))
        destination_name = self._normalize_name(local_connector, destination_name)
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
        is_thread_category: bool = False,
        category_from_channel_id: str | None = None,
        destination_category: str | None = None,
        new_name: str | None = None,
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
        Category there too. If the source channel's Category is itself already
        linked (via `/link category`) to a Category on `destination`, the
        mirrored channel lands in *that* linked Category rather than a fresh
        same-named one (issue #50) - so `/mirror channel` (both directions and
        `all`) respects linked Categories the same way `/mirror channel from`
        already did. `is_thread_category` is only ever
        True from DiscordSenderService._handle_thread_create's auto-mirror -
        it marks that destination Category as thread-only, so
        `/link-category` later refuses to link it.

        Raises LinkError (aborting the whole operation, `all` included) when
        the connector's can_view_channel hook says for certain the bridge bot
        can't see `local_channel_id` - mirroring a channel the bot can't see
        otherwise creates a stub named after the platform's hidden-channel
        placeholder (issue #33).

        `category_from_channel_id`, if given, is a channel id on
        `local_connector` (the thread's parent channel) whose linked
        counterpart's name *on `destination`* becomes the Category title
        instead of `local_channel_category` - so the Category is named after
        the destination's own copy of the parent channel (a Discord
        `bot-config` thread lands under Stoat's "Bot Config"), not the
        Discord name. Falls back to `local_channel_category` when the parent
        has no linked channel on `destination`.

        `destination_category`, if given, is a Category id or name *on
        `destination`* that the mirrored channel is placed under - it overrides
        linked-Category resolution and same-name matching entirely (issue #75):
        `category_from_channel_id` and the `/link category` lookup are both
        skipped. An id is resolved to its title (so `ensure_channel` doesn't
        create a Category named after the id); a bare name that doesn't resolve
        is get-or-created by `ensure_channel` as-is. `mirror_channel_from` routes
        its own `[category]` (a *local* Category) through here, since its
        `destination` is the local connector.

        `new_name`, if given, is the name to create/find the counterpart under
        on `destination` instead of carrying `local_channel_name` over -
        destination-normalised by `ensure_channel` and matched the same way
        (issue #44)."""
        if destination not in self._connectors:
            raise LinkError(f"'{destination}' isn't a known connector.")
        if destination == local_connector:
            raise LinkError("can't mirror a channel to its own connector.")

        target_name = _clean_new_name(new_name) or local_channel_name

        local_channel_id = await self._resolve_to_id(local_connector, local_channel_id)

        if await self._channel_is_hidden(local_connector, local_channel_id):
            raise LinkError(
                f"the bridge bot can't see channel '{local_channel_id}' on "
                f"{self._connectors[local_connector].label} - give it access to that channel first."
            )

        bridge_group = await self._channel_mappings.get_bridge_group(local_connector, local_channel_id)
        if bridge_group is not None:
            existing = await self._channel_mappings.get_mapped_channels(bridge_group)
            if any(m.connector_id == destination for m in existing):
                return f"{self._connectors[destination].label}: already synced - skipped."

        dest_info = self._connectors[destination]
        if dest_info.ensure_channel is None:
            return f"{dest_info.label}: doesn't support channel creation - link it manually with /link channel."

        category = local_channel_category
        category_parent_channel_id: str | None = None
        explicit_category = _clean_new_name(destination_category)
        if explicit_category is not None:
            # An explicit Category on `destination` wins over every other
            # source of a Category name - linked Categories included (issue #75).
            category = await self._resolve_destination_category_name(destination, explicit_category)
        elif category_from_channel_id is not None:
            linked_parent = await self._linked_channel(
                local_connector, category_from_channel_id, destination
            )
            if linked_parent is not None:
                category_parent_channel_id = linked_parent.channel_id
                if linked_parent.channel_name:
                    category = linked_parent.channel_name
        else:
            # Respect linked Categories: if `local_channel_id`'s Category on
            # `local_connector` is already linked to one on `destination`,
            # land the mirrored channel in that linked Category (by its name
            # on `destination`) instead of a fresh same-named one (issue #50).
            linked_category = await self._local_category_for_source_channel(
                destination, local_connector, local_channel_id
            )
            if linked_category is not None:
                category = linked_category

        # Cosmetic metadata (description / maturity / icon) off the source
        # channel, so the mirrored channel isn't created blank (issue #32).
        # Best-effort - a missing hook or a raising one just means no
        # metadata is carried. Only passed on when there's something to pass:
        # keeps `ensure_channel` callers that don't take the keyword (older
        # test fakes) untouched, and lets each hook apply it on create only.
        metadata = None
        src_info = self._connectors.get(local_connector)
        if src_info is not None and src_info.describe_channel is not None:
            try:
                metadata = await src_info.describe_channel(local_channel_id)
            except Exception as exc:
                logger.warning(
                    "mirror channel: %s.describe_channel(%r) failed: %s", local_connector, local_channel_id, exc
                )
        extra = {"metadata": metadata} if metadata is not None else {}

        try:
            destination_channel_id = await dest_info.ensure_channel(
                target_name, category, is_thread_category, category_parent_channel_id, **extra
            )
        except Exception as exc:
            logger.warning("mirror channel: %s.ensure_channel(%r) failed: %s", destination, target_name, exc)
            return f"{dest_info.label}: failed to create/find a channel: {exc}"

        try:
            return await self.link_channel(
                local_connector=destination,
                local_channel_id=destination_channel_id,
                local_channel_name=target_name,
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
        is_thread_category: bool = False,
        category_from_channel_id: str | None = None,
    ) -> str:
        """`/mirror channel all` - mirror_channel() against every other
        configured connector, one line of summary/skip/error per connector
        rather than stopping at the first problem."""
        results = [
            await self.mirror_channel(
                local_connector=local_connector,
                local_channel_id=local_channel_id,
                local_channel_name=local_channel_name,
                destination=destination,
                local_channel_category=local_channel_category,
                is_thread_category=is_thread_category,
                category_from_channel_id=category_from_channel_id,
            )
            for destination in self._connectors
            if destination != local_connector
        ]
        return "\n".join(results) if results else "no other connectors configured."

    async def mirror_channel_from(
        self,
        *,
        local_connector: str,
        source: str,
        source_id: str,
        new_name: str | None = None,
        local_category: str | None = None,
    ) -> str:
        """`/mirror channel from <source> <source_id>` - the inbound
        direction: `source`'s `source_id` channel already exists, so create a
        linked counterpart *here* on `local_connector` and link the two.

        Mechanically this is `mirror_channel` with the connectors swapped
        (push `source`'s channel to `local_connector`), which gets bridge-
        group reuse for free via `link_channel` and linked-Category respect
        for free via `mirror_channel`'s own resolution (issue #50): if the
        source channel sits in a Category that's already linked (via
        `/link category`) to a Category here, the new local channel is placed
        into *that* linked Category rather than a fresh same-named one.

        `local_category`, if given, is a Category id or name here on
        `local_connector` that the new channel is placed under instead -
        overriding the linked-Category resolution above (issue #75). It's
        forwarded as `mirror_channel`'s `destination_category` (whose
        `destination` in this swapped call *is* `local_connector`).

        `new_name`, if given, names the freshly-created local channel instead
        of carrying the source channel's name over (issue #44)."""
        if source not in self._connectors:
            raise LinkError(f"'{source}' isn't a known connector.")
        if source == local_connector:
            raise LinkError("can't mirror a channel from a connector to itself.")

        source_id = await self._resolve_to_id(source, source_id)
        source_name = await self._resolve_name(source, source_id)

        return await self.mirror_channel(
            local_connector=source,
            local_channel_id=source_id,
            local_channel_name=source_name,
            destination=local_connector,
            destination_category=local_category,
            new_name=new_name,
        )

    async def _resolve_destination_category_name(self, connector: str, token: str) -> str:
        """Resolve a Category id-or-name `token` on `connector` to the Category
        *title* `ensure_channel` places a channel under. A name is resolved to
        its id then back to the canonical title; a bare id is turned into its
        title directly. A `token` that resolves to nothing is passed straight
        through as a title for `ensure_channel` to get-or-create - *unless* it's
        shaped like a platform id (all-digit Discord snowflake, 26-char ULID),
        which then raises rather than spawning a Category literally named after
        an id nothing matched (the issue #64 failure mode)."""
        info = self._connectors.get(connector)
        if info is None:
            return token
        category_id = token
        if info.resolve_category_id_by_name is not None:
            try:
                resolved = await info.resolve_category_id_by_name(token)
            except Exception:
                logger.debug("couldn't resolve category name %r on %s", token, connector, exc_info=True)
                resolved = None
            if resolved:
                category_id = resolved
        if info.resolve_category_name is not None:
            try:
                name = await info.resolve_category_name(category_id)
            except Exception:
                logger.debug("couldn't resolve category id %r on %s", category_id, connector, exc_info=True)
                name = None
            if name:
                return name
        if _BARE_ID_RE.match(token):
            raise LinkError(
                f"couldn't find a Category matching '{token}' on {info.label} - "
                "pass an existing Category's id/name, or a name to create."
            )
        return token

    async def _local_category_for_source_channel(
        self, local_connector: str, source: str, source_channel_id: str
    ) -> str | None:
        """The name, on `local_connector`, of the Category that
        `source_channel_id`'s Category (on `source`) is linked to via
        `/link category` - so `mirror_channel` / `mirror_channel_from` land the
        new channel there - or the source Category's own name if it isn't
        linked, or None if the source channel is uncategorised / unresolvable.
        `source` and `local_connector` are just "the connector the channel is
        on" and "the connector we want the linked Category name on"; either
        direction of `/mirror channel` fills them in."""
        info = self._connectors.get(source)
        if info is None or info.resolve_channel_category is None:
            return None
        try:
            resolved = await info.resolve_channel_category(source_channel_id)
        except Exception:
            logger.debug("couldn't resolve category for channel %s on %s", source_channel_id, source, exc_info=True)
            return None
        if not resolved:
            return None
        source_category_id, source_category_name = resolved
        if self._category_mappings is not None:
            group = await self._category_mappings.get_bridge_group(source, source_category_id)
            if group is not None:
                mapped = await self._category_mappings.get_mapped_categories(group)
                local = next((m for m in mapped if m.connector_id == local_connector), None)
                if local is not None and local.category_name:
                    return local.category_name
        return source_category_name or None

    async def list_linked_channels(self, *, local_connector: str, local_channel_id: str) -> str:
        """Human-readable listing of every channel bridged to
        `local_channel_id` on `local_connector` (the invoking channel),
        across every connector in its bridge group - for the
        `/linked channels` command. Read-only, so unlike `link_channel` it
        never raises LinkError; an unlinked channel just gets a plain
        "nothing here" reply."""
        local_channel_id = await self._resolve_to_id(local_connector, local_channel_id)
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
        """`/unlink channel`. `destination` (a connector id) kicks just that
        one member out of `local_channel_id`'s bridge group - everyone else
        (including this channel) stays linked to each other; None/"all"
        (the default) dissolves the whole group instead, unlinking every
        member. Raises LinkError if the channel isn't linked, or
        `destination` isn't actually a member of its group.

        Every channel that ends up with *no* linked counterparts left - the
        kicked one, and any lone survivor a kick strands - is announced to
        its connector via the on_channel_unlinked hook (IRC uses it to post a
        "this channel was unlinked from ..." notice and PART); a channel that
        still has other links stays untouched and unannounced."""
        local_channel_id = await self._resolve_to_id(local_connector, local_channel_id)
        bridge_group = await self._channel_mappings.get_bridge_group(local_connector, local_channel_id)
        if bridge_group is None:
            raise LinkError("this channel isn't linked to anything.")
        mapped = await self._channel_mappings.get_mapped_channels(bridge_group)

        if destination is None or destination.lower() == "all":
            count = await self._channel_mappings.delete_bridge_group(bridge_group)
            await self._announce_unlinked(mapped, removed=mapped)
            return f"Unlinked this channel's entire bridge group ({count} channel(s) removed)."

        target = next((m for m in mapped if m.connector_id == destination), None)
        if target is None:
            raise LinkError(f"'{destination}' isn't linked in this channel's bridge group.")
        await self._channel_mappings.delete_mapping(destination, target.channel_id)
        survivors = [m for m in mapped if m.connector_id != destination]
        if len(survivors) <= 1:
            # only one member (or none) would be left - a group of one isn't a
            # bridge, so dissolve it fully and announce every former member.
            for m in survivors:
                await self._channel_mappings.delete_mapping(m.connector_id, m.channel_id)
            await self._announce_unlinked(mapped, removed=mapped)
        else:
            await self._announce_unlinked(mapped, removed=[target])
        label = self._connectors[destination].label if destination in self._connectors else destination
        return f"Unlinked {label} channel '{target.channel_name}' ({target.channel_id}) from this bridge group."

    async def _announce_unlinked(self, members: list[ChannelMapping], *, removed: list[ChannelMapping]) -> None:
        """Fire the on_channel_unlinked hook for each channel in `removed`,
        telling it which of the other `members` it's no longer bridged to."""
        for m in removed:
            others = [
                x for x in members if (x.connector_id, x.channel_id) != (m.connector_id, m.channel_id)
            ]
            labels = ", ".join(
                f"{self._connectors[x.connector_id].label if x.connector_id in self._connectors else x.connector_id}"
                f" '{x.channel_name}'"
                for x in others
            )
            await self._notify_unlinked(m.connector_id, m.channel_id, labels)

    async def is_linked(self, connector_id: str, channel_id: str) -> bool:
        """Whether `channel_id` (on `connector_id`) already belongs to a
        bridge group - used by Discord's thread-creation auto-mirror (see
        DiscordSenderService._handle_thread_create) to gate on the thread's
        parent channel already being bridged, rather than mirroring every
        thread created anywhere in the guild."""
        return await self._channel_mappings.get_bridge_group(connector_id, channel_id) is not None

    async def _linked_channel(
        self, local_connector: str, channel_id: str, destination: str
    ) -> ChannelMapping | None:
        """`channel_id`'s linked counterpart mapping on `destination` (from
        the channel bridge group `channel_id` belongs to on
        `local_connector`), or None if it isn't linked there. Used by
        mirror_channel to name a thread Category after - and bind it to - the
        destination's own copy of the thread's parent channel."""
        bridge_group = await self._channel_mappings.get_bridge_group(local_connector, channel_id)
        if bridge_group is None:
            return None
        mapped = await self._channel_mappings.get_mapped_channels(bridge_group)
        return next((m for m in mapped if m.connector_id == destination), None)

    async def _resolve_to_id(self, connector: str, token: str) -> str:
        """Resolve a bare channel name to its native id so the channel
        commands accept either. A token the hook doesn't recognize (or a
        connector with no hook - e.g. IRC, where a channel id is already
        `#name`) is returned unchanged. Mirrors RoleLinker._resolve_to_id."""
        info = self._connectors.get(connector)
        if info is not None and info.resolve_channel_id_by_name is not None:
            try:
                channel_id = await info.resolve_channel_id_by_name(token)
            except Exception:
                logger.debug("couldn't resolve channel name %r on %s", token, connector, exc_info=True)
                channel_id = None
            if channel_id:
                return channel_id
        return token

    def _normalize_name(self, connector_id: str, name: str) -> str:
        """Fold `name` into the shape `connector_id` stores channel names under
        (IRC's `#`-prefix sterilization), so a name carried over by
        `/mirror channel` matches the id `ensure_channel` produced (issue #51).
        A connector with no `normalize_channel_name` hook - everything but IRC -
        leaves the name untouched."""
        info = self._connectors.get(connector_id)
        if info is None or info.normalize_channel_name is None:
            return name
        try:
            return info.normalize_channel_name(name)
        except Exception:
            logger.debug("couldn't normalize channel name %r on %s", name, connector_id, exc_info=True)
            return name

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

    async def _channel_is_hidden(self, connector_id: str, channel_id: str) -> bool:
        """True only when the connector's can_view_channel hook says, for
        certain, that the bridge bot can't see `channel_id`. A missing hook,
        an error, or a "can't tell" (None) all return False - the guard is
        deliberately narrow so it never blocks a mirror on a shaky signal
        (see mirror_channel / issue #33)."""
        info = self._connectors.get(connector_id)
        if info is None or info.can_view_channel is None:
            return False
        try:
            visible = await info.can_view_channel(channel_id)
        except Exception:
            logger.debug("can_view_channel(%s) failed on %s", channel_id, connector_id, exc_info=True)
            return False
        return visible is False

    async def _notify_linked(self, connector_id: str, channel_id: str) -> None:
        info = self._connectors.get(connector_id)
        if info is None or info.on_channel_linked is None:
            return
        await info.on_channel_linked(channel_id)

    async def _notify_unlinked(self, connector_id: str, channel_id: str, unlinked_from: str) -> None:
        info = self._connectors.get(connector_id)
        if info is None or info.on_channel_unlinked is None:
            return
        await info.on_channel_unlinked(channel_id, unlinked_from)


class CategoryLinker:
    """The Category-level counterpart of ChannelLinker: `/link category`
    links two Categories across connectors, and once linked, a new channel
    appearing inside either is auto-synced (created + linked) onto the
    other's own linked Category - see sync_new_channel, called from each
    connector's own channel-create event handler
    (DiscordSenderService._handle_channel_create,
    StoatSenderService._handle_channel_create)."""

    def __init__(
        self,
        category_mappings: CategoryMappingRepository,
        thread_categories: ThreadCategoryRepository,
        channel_linker: ChannelLinker,
        connectors: dict[str, ConnectorInfo],
    ) -> None:
        self._category_mappings = category_mappings
        self._thread_categories = thread_categories
        self._channel_linker = channel_linker
        self._connectors = connectors

    @property
    def connectors(self) -> dict[str, ConnectorInfo]:
        return self._connectors

    async def link_category(
        self,
        *,
        local_connector: str,
        local_category_id: str | None,
        local_category_name: str,
        source: str,
        source_id: str,
        destination_id: str | None,
    ) -> str:
        """Link `source`'s `source_id` Category to `destination_id` (or the
        invoking Category, if omitted) on `local_connector`. Symmetric to
        ChannelLinker.link_channel, plus a guard rejecting either side if
        it's a Category Discord's thread/forum-post auto-mirroring created
        (see DiscordSenderService._handle_thread_create) - such a Category
        is dedicated to that per-thread-parent mirroring flow, and linking
        it here would create a second, conflicting sync path onto the same
        channels. Once linked, any new channel that appears in either
        Category is auto-synced onto the other - see sync_new_channel."""
        if source not in self._connectors:
            raise LinkError(f"'{source}' isn't a known connector.")

        source_id = await self._resolve_to_id(source, source_id)
        if destination_id is not None:
            destination_id = await self._resolve_to_id(local_connector, destination_id)
        if destination_id is None and local_category_id is None:
            raise LinkError("this channel isn't inside a Category.")

        if destination_id is None or destination_id == local_category_id:
            destination_category_id = local_category_id
            destination_name = local_category_name
        else:
            destination_category_id = destination_id
            destination_name = await self._resolve_name(local_connector, destination_id)

        if source == local_connector and source_id == destination_category_id:
            raise LinkError("can't link a Category to itself.")

        if await self._thread_categories.is_thread_category(source, source_id) or await self._thread_categories.is_thread_category(
            local_connector, destination_category_id
        ):
            raise LinkError(
                "that Category was auto-created for Discord thread mirroring and can't be linked with /link category."
            )

        source_group = await self._category_mappings.get_bridge_group(source, source_id)
        destination_group = await self._category_mappings.get_bridge_group(local_connector, destination_category_id)
        if source_group and destination_group and source_group != destination_group:
            raise LinkError(
                "both Categories are already linked, but to different bridge groups - unlink one before relinking."
            )
        bridge_group = source_group or destination_group or uuid.uuid4().hex

        source_name = await self._resolve_name(source, source_id)
        await self._category_mappings.upsert(
            CategoryMapping(bridge_group=bridge_group, connector_id=source, category_id=source_id, category_name=source_name)
        )
        await self._category_mappings.upsert(
            CategoryMapping(
                bridge_group=bridge_group,
                connector_id=local_connector,
                category_id=destination_category_id,
                category_name=destination_name,
            )
        )

        source_label = self._connectors[source].label
        local_info = self._connectors.get(local_connector)
        local_label = local_info.label if local_info else local_connector
        return (
            f"Linked {source_label} Category '{source_name}' ({source_id}) to "
            f"{local_label} Category '{destination_name}' ({destination_category_id}). "
            "New channels in either will now sync automatically."
        )

    async def list_linked_categories(
        self, *, local_connector: str, local_category_id: str | None = None, local_category: str | None = None
    ) -> str:
        """Read-only listing, for `/linked categories` - never raises
        LinkError, same as ChannelLinker.list_linked_channels. `local_category`
        (an id or a bare name) overrides `local_category_id` (the invoking
        channel's Category) when given."""
        if local_category is not None:
            local_category_id = await self._resolve_to_id(local_connector, local_category)
        if local_category_id is None:
            return "This channel isn't inside a Category."
        bridge_group = await self._category_mappings.get_bridge_group(local_connector, local_category_id)
        if bridge_group is None:
            return "This Category isn't linked to any others."

        mapped = await self._category_mappings.get_mapped_categories(bridge_group)
        lines = []
        for mapping in sorted(mapped, key=lambda m: (m.connector_id, m.category_id)):
            info = self._connectors.get(mapping.connector_id)
            label = info.label if info else mapping.connector_id
            marker = (
                " (this Category)"
                if mapping.connector_id == local_connector and mapping.category_id == local_category_id
                else ""
            )
            lines.append(f"{label}: {mapping.category_name} ({mapping.category_id}){marker}")
        return "Linked Categories:\n" + "\n".join(lines)

    async def unlink_category(
        self,
        *,
        local_connector: str,
        local_category_id: str | None = None,
        local_category: str | None = None,
        destination: str | None,
    ) -> str:
        """`/unlink category`, symmetric to ChannelLinker.unlink_channel.
        `local_category` (an id or a bare name) overrides `local_category_id`
        (the invoking channel's Category) when given."""
        if local_category is not None:
            local_category_id = await self._resolve_to_id(local_connector, local_category)
        if local_category_id is None:
            raise LinkError("this channel isn't inside a Category.")
        bridge_group = await self._category_mappings.get_bridge_group(local_connector, local_category_id)
        if bridge_group is None:
            raise LinkError("this Category isn't linked to anything.")

        if destination is None or destination.lower() == "all":
            count = await self._category_mappings.delete_bridge_group(bridge_group)
            return f"Unlinked this Category's entire bridge group ({count} Category(s) removed)."

        mapped = await self._category_mappings.get_mapped_categories(bridge_group)
        target = next((m for m in mapped if m.connector_id == destination), None)
        if target is None:
            raise LinkError(f"'{destination}' isn't linked in this Category's bridge group.")
        await self._category_mappings.delete_mapping(destination, target.category_id)
        label = self._connectors[destination].label if destination in self._connectors else destination
        return f"Unlinked {label} Category '{target.category_name}' ({target.category_id}) from this bridge group."

    async def mirror_category(
        self,
        *,
        local_connector: str,
        local_category_id: str | None = None,
        local_category: str | None = None,
        local_category_name: str | None = None,
        destination: str,
        new_name: str | None = None,
    ) -> str:
        """Ensure `local_category` (id or bare name, on `local_connector`) has
        a linked counterpart Category on `destination`: reuses the existing
        linked Category there if the pair is already linked, otherwise creates
        a same-named one via `destination`'s ensure_category() hook and links
        it. Then relocates every channel inside the source Category onto that
        destination Category - a child already linked to a `destination`
        channel is *moved* into it (move_channel_to_category hook), an
        unlinked child is mirrored (created + linked) there via
        ChannelLinker.mirror_channel. Reports rather than raises per problem so
        `mirror_category_all` can carry on past one bad destination.

        `new_name`, if given, is the title to create/find the counterpart
        Category under on `destination` instead of the source Category's title
        (issue #44); it doesn't rename any mirrored child channels."""
        if destination not in self._connectors:
            raise LinkError(f"'{destination}' isn't a known connector.")
        if destination == local_connector:
            raise LinkError("can't mirror a Category to its own connector.")

        if local_category is not None:
            local_category_id = await self._resolve_to_id(local_connector, local_category)
        if local_category_id is None:
            raise LinkError("this channel isn't inside a Category.")
        source_name = local_category_name or await self._resolve_name(local_connector, local_category_id)
        target_name = _clean_new_name(new_name) or source_name

        dest_info = self._connectors[destination]
        dest_label = dest_info.label

        bridge_group = await self._category_mappings.get_bridge_group(local_connector, local_category_id)
        dest_category_id: str | None = None
        match = None
        if bridge_group is not None:
            existing = await self._category_mappings.get_mapped_categories(bridge_group)
            match = next((m for m in existing if m.connector_id == destination), None)
            if match is not None:
                dest_category_id = match.category_id

        # Resolve the destination Category's name, but fall back to a known-good
        # `fallback` rather than echoing the raw id when the lookup comes up
        # empty. `ensure_category` just created-or-matched the Category as
        # `target_name`, and the connector's cache won't show a brand-new one
        # yet, so `_resolve_name` would hand back the id - which then gets
        # stored as the Category's name and, worse, passed to child-channel
        # placement as a Category *title*, spawning a second Category literally
        # named after the id (issue #64).
        async def _dest_name(fallback: str) -> str:
            resolved = await self._resolve_name(destination, dest_category_id)
            return resolved if resolved != dest_category_id else fallback

        lines: list[str] = []
        if dest_category_id is None:
            if dest_info.ensure_category is None:
                return f"{dest_label}: doesn't support Category creation - link it manually with /link category."
            try:
                dest_category_id = await dest_info.ensure_category(target_name)
            except Exception as exc:
                logger.warning("mirror-category: %s.ensure_category(%r) failed: %s", destination, target_name, exc)
                return f"{dest_label}: failed to create/find a Category: {exc}"
            try:
                lines.append(
                    await self.link_category(
                        local_connector=destination,
                        local_category_id=dest_category_id,
                        local_category_name=await _dest_name(target_name),
                        source=local_connector,
                        source_id=local_category_id,
                        destination_id=None,
                    )
                )
            except LinkError as exc:
                return f"{dest_label}: {exc}"
        else:
            lines.append(f"{dest_label}: already linked - reusing '{dest_category_id}'.")

        dest_category_name = await _dest_name(
            (match.category_name if match is not None else None) or target_name
        )
        info = self._connectors.get(local_connector)
        if info is not None and info.channels_in_category is not None:
            try:
                children = await info.channels_in_category(local_category_id)
            except Exception:
                logger.debug("mirror-category: channels_in_category failed for %s", local_connector, exc_info=True)
                children = []
            for cid, cname in children:
                try:
                    linked = await self._channel_linker._linked_channel(local_connector, cid, destination)
                    if linked is not None and dest_info.move_channel_to_category is not None:
                        await dest_info.move_channel_to_category(linked.channel_id, dest_category_id)
                        lines.append(f"{dest_label}: moved '{cname}' into the Category.")
                    else:
                        lines.append(
                            await self._channel_linker.mirror_channel(
                                local_connector=local_connector,
                                local_channel_id=cid,
                                local_channel_name=cname,
                                destination=destination,
                                local_channel_category=dest_category_name,
                            )
                        )
                except Exception as exc:
                    logger.warning("mirror-category: child %r -> %s failed: %s", cname, destination, exc)
                    lines.append(f"{dest_label}: '{cname}' failed: {exc}")
        return "\n".join(lines)

    async def mirror_category_all(
        self,
        *,
        local_connector: str,
        local_category_id: str | None = None,
        local_category: str | None = None,
        local_category_name: str | None = None,
    ) -> str:
        """`/mirror category <local> all` - mirror_category() against every
        other configured connector."""
        results = [
            await self.mirror_category(
                local_connector=local_connector,
                local_category_id=local_category_id,
                local_category=local_category,
                local_category_name=local_category_name,
                destination=destination,
            )
            for destination in self._connectors
            if destination != local_connector
        ]
        return "\n".join(r for r in results if r) if results else "no other connectors configured."

    async def mirror_category_from(
        self, *, local_connector: str, source: str, source_id: str, new_name: str | None = None
    ) -> str:
        """`/mirror category from <source> <source_id>` - `source`'s Category
        already exists; create a linked counterpart *here* on
        `local_connector`, link them, and relocate/mirror the source
        Category's channels into it. `mirror_category` with the connectors
        swapped.

        `new_name`, if given, titles the local counterpart Category instead of
        carrying the source Category's title over (issue #44)."""
        if source not in self._connectors:
            raise LinkError(f"'{source}' isn't a known connector.")
        if source == local_connector:
            raise LinkError("can't mirror a Category from a connector to itself.")
        return await self.mirror_category(
            local_connector=source, local_category=source_id, destination=local_connector, new_name=new_name
        )

    async def sync_new_channel(
        self, *, local_connector: str, local_category_id: str, channel_id: str, channel_name: str
    ) -> None:
        """Called by each connector's channel-create event handler when a
        new channel appears inside `local_category_id`. If that Category is
        linked (via /link-category), auto-mirrors the new channel onto every
        other connector in its bridge group - into that destination's own
        linked Category (by name), not `local_category_id`'s name, since
        /link-category allows differently-named Categories across
        connectors (unlike /mirror channel's same-name carry-over). No-op if
        the Category isn't linked - which a thread-mirroring-created
        Category never is, since link_category refuses to ever link one, so
        this is naturally never triggered for those without needing its own
        explicit guard. Reuses ChannelLinker.mirror_channel, whose own
        "already synced - skipped" check makes this safe against duplicate/
        echoed channel-create events (e.g. the bridge's own created channel
        firing its creator's event back at this same listener)."""
        bridge_group = await self._category_mappings.get_bridge_group(local_connector, local_category_id)
        if bridge_group is None:
            return
        mapped = await self._category_mappings.get_mapped_categories(bridge_group)
        for mapping in mapped:
            if mapping.connector_id == local_connector:
                continue
            result = await self._channel_linker.mirror_channel(
                local_connector=local_connector,
                local_channel_id=channel_id,
                local_channel_name=channel_name,
                destination=mapping.connector_id,
                local_channel_category=mapping.category_name,
            )
            logger.info(
                "[category-sync] new channel %r in %s's linked Category -> %s: %s",
                channel_name,
                local_connector,
                mapping.connector_id,
                result,
            )

    async def bind_thread_category(
        self, connector_id: str, parent_channel_id: str, category_id: str
    ) -> None:
        """Bind `parent_channel_id` (on `connector_id`) to the Stoat Category
        `category_id` auto-created for its Discord threads - called from a
        connector's ensure_channel() when it was itself called with
        is_thread_category=True (ultimately from
        DiscordSenderService._handle_thread_create). Later threads for the
        same parent resolve the Category by this id, not by title."""
        await self._thread_categories.bind(connector_id, parent_channel_id, category_id)

    async def thread_category_id(self, connector_id: str, parent_channel_id: str) -> str | None:
        """The Category id bound to `parent_channel_id` on `connector_id`, or
        None if no thread Category has been created for it yet."""
        return await self._thread_categories.get_category_id(connector_id, parent_channel_id)

    async def thread_category_parent(self, connector_id: str, category_id: str) -> str | None:
        """The parent channel id bound to thread Category `category_id` on
        `connector_id` - the reverse lookup, used to group the parent channel
        into its thread Category by id rather than by name match."""
        return await self._thread_categories.get_parent_channel_id(connector_id, category_id)

    async def forget_thread_category(self, connector_id: str, parent_channel_id: str) -> None:
        """Drop `parent_channel_id`'s binding on `connector_id` - its bound
        Category is gone from the server, so the next thread rebinds it."""
        await self._thread_categories.forget(connector_id, parent_channel_id)

    async def is_thread_category(self, connector_id: str, category_id: str) -> bool:
        """Whether `category_id` on `connector_id` was auto-created for
        Discord thread/forum-post mirroring - the read side of
        bind_thread_category, used by `/link-category` to refuse linking it
        and by StoatSenderService to decide whether to group a thread
        Category's parent channel into it."""
        return await self._thread_categories.is_thread_category(connector_id, category_id)

    async def _resolve_to_id(self, connector: str, token: str) -> str:
        info = self._connectors.get(connector)
        if info is not None and info.resolve_category_id_by_name is not None:
            try:
                category_id = await info.resolve_category_id_by_name(token)
            except Exception:
                logger.debug("couldn't resolve category name %r on %s", token, connector, exc_info=True)
                category_id = None
            if category_id:
                return category_id
        return token

    async def _resolve_name(self, connector_id: str, category_id: str) -> str:
        info = self._connectors.get(connector_id)
        if info is None or info.resolve_category_name is None:
            return category_id
        try:
            name = await info.resolve_category_name(category_id)
        except Exception:
            logger.debug("couldn't resolve category name for %s on %s", category_id, connector_id, exc_info=True)
            return category_id
        return name or category_id


class EmoteLinker:
    """`/link emote` / `/mirror emote` / `/unlink emote` / `/linked emotes` -
    the custom-emoji counterpart of RoleLinker, backed by
    EmojiMappingRepository (the same store reaction/emoji sync uses).

    Emoji are Discord/Stoat only; IRC has no custom-emoji concept, so no
    connector there registers any emoji hook and the emote commands aren't
    offered.

    Every id argument also accepts a bare emoji NAME - resolved to an id via
    the connector's resolve_emoji_id_by_name hook, falling back to treating
    the token as an id if the hook is absent or comes up empty.
    """

    def __init__(self, emoji_mappings: EmojiMappingRepository, connectors: dict[str, ConnectorInfo]) -> None:
        self._emoji_mappings = emoji_mappings
        self._connectors = connectors

    @property
    def connectors(self) -> dict[str, ConnectorInfo]:
        return self._connectors

    async def link_emote(self, *, local_connector: str, local_id: str, source: str, source_id: str) -> str:
        """Link `source`'s emoji to a local emoji on `local_connector`. Both
        emoji arguments accept an id or a bare name. Raises LinkError if
        `source` is unknown, the two are the same emoji, or both already
        belong to two *different* existing mapping groups."""
        if source not in self._connectors:
            raise LinkError(f"'{source}' isn't a known connector.")

        source_id = await self._resolve_to_id(source, source_id)
        local_id = await self._resolve_to_id(local_connector, local_id)
        if source == local_connector and source_id == local_id:
            raise LinkError("can't link an emote to itself.")

        source_group = await self._emoji_mappings.get_group_id(source, source_id)
        local_group = await self._emoji_mappings.get_group_id(local_connector, local_id)
        if source_group and local_group and source_group != local_group:
            raise LinkError(
                "both emotes are already linked, but to different mapping groups - unlink one before relinking."
            )

        source_name = await self._resolve_name(source, source_id)
        local_name = await self._resolve_name(local_connector, local_id)
        source_ref = EmojiRef(connector_id=source, emoji_id=source_id, name=source_name)
        local_ref = EmojiRef(connector_id=local_connector, emoji_id=local_id, name=local_name)

        if source_group is None and local_group is None:
            group_id = await self._emoji_mappings.try_reserve(source_ref)
            if group_id is None:
                # lost a race to a concurrent reservation - fall back to whatever group now owns it
                group_id = await self._emoji_mappings.get_group_id(source, source_id)
            await self._emoji_mappings.add_refs(group_id, [local_ref])
        elif local_group is None:
            await self._emoji_mappings.add_refs(source_group, [local_ref])
        elif source_group is None:
            await self._emoji_mappings.add_refs(local_group, [source_ref])
        # else: source_group == local_group already - no-op, already linked

        source_label = self._connectors[source].label
        local_info = self._connectors.get(local_connector)
        local_label = local_info.label if local_info else local_connector
        return f"Linked {source_label} emote '{source_name}' to {local_label} emote '{local_name}'."

    async def mirror_emote(
        self, *, local_connector: str, local_emote: str, destination: str, new_name: str | None = None
    ) -> str:
        """Ensure `local_emote` (id or bare name, on `local_connector`) has a
        linked counterpart on `destination`: reuses the existing link if the
        pair is already linked, otherwise reads the source emoji's image via
        `local_connector`'s resolve_emoji hook, recreates it on `destination`
        via its ensure_emoji hook, and links the two. Reports rather than
        raises per problem so `mirror_emote_all` can carry on past one bad
        destination.

        `new_name`, if given, is the name the counterpart emoji is
        created/matched under on `destination` instead of the source emoji's
        name (issue #44)."""
        if destination not in self._connectors:
            raise LinkError(f"'{destination}' isn't a known connector.")
        if destination == local_connector:
            raise LinkError("can't mirror an emote to its own connector.")

        source_id = await self._resolve_to_id(local_connector, local_emote)
        source_name = await self._resolve_name(local_connector, source_id)
        target_name = _clean_new_name(new_name) or source_name
        dest_info = self._connectors[destination]

        group_id = await self._emoji_mappings.get_group_id(local_connector, source_id)
        if group_id is not None:
            refs = await self._emoji_mappings.get_refs(group_id)
            if any(r.connector_id == destination for r in refs):
                return f"{dest_info.label}: already synced - skipped."

        # Prefer linking to a same-named emote that already exists on the
        # destination over creating a duplicate (mirrors /mirror role's
        # create-or-match). Name only - we can't compare images.
        if dest_info.resolve_emoji_id_by_name is not None and target_name:
            try:
                existing_id = await dest_info.resolve_emoji_id_by_name(target_name)
            except Exception:
                logger.debug("mirror-emote: %s.resolve_emoji_id_by_name(%r) failed", destination, target_name, exc_info=True)
                existing_id = None
            if existing_id:
                try:
                    return await self.link_emote(
                        local_connector=destination, local_id=existing_id, source=local_connector, source_id=source_id
                    )
                except LinkError as exc:
                    return f"{dest_info.label}: {exc}"

        source_info = self._connectors.get(local_connector)
        if source_info is None or source_info.resolve_emoji is None:
            return f"{dest_info.label}: can't read {local_connector}'s emoji to copy it."
        if dest_info.ensure_emoji is None:
            return f"{dest_info.label}: doesn't support emoji creation - link it manually with /link emote."

        try:
            custom_emoji = await source_info.resolve_emoji(source_id)
        except Exception as exc:
            logger.warning("mirror-emote: %s.resolve_emoji(%r) failed: %s", local_connector, source_id, exc)
            return f"{dest_info.label}: couldn't read the source emoji: {exc}"
        if custom_emoji is None:
            return f"{dest_info.label}: source emoji '{source_name}' not found."
        if target_name != custom_emoji.name:
            # An emoji can't be created name-only (unlike a role) - hand
            # ensure_emoji the same CustomEmoji with just the name swapped so
            # the recreated copy takes `new_name` (issue #44).
            custom_emoji = replace(custom_emoji, name=target_name)

        try:
            created = await dest_info.ensure_emoji(custom_emoji)
        except Exception as exc:
            logger.warning("mirror-emote: %s.ensure_emoji(%r) failed: %s", destination, target_name, exc)
            return f"{dest_info.label}: failed to create the emoji: {exc}"
        if created is None:
            return f"{dest_info.label}: couldn't create the emoji (slots full, name rejected, image too large?)."

        try:
            return await self.link_emote(
                local_connector=destination, local_id=created.native_id, source=local_connector, source_id=source_id
            )
        except LinkError as exc:
            return f"{dest_info.label}: {exc}"

    async def mirror_emote_all(self, *, local_connector: str, local_emote: str) -> str:
        """`/mirror emote <local> all` - mirror_emote() against every other
        configured connector, one line of summary/skip/error per connector."""
        results = [
            await self.mirror_emote(local_connector=local_connector, local_emote=local_emote, destination=destination)
            for destination in self._connectors
            if destination != local_connector
        ]
        return "\n".join(r for r in results if r) if results else "no other connectors configured."

    async def mirror_emote_from(
        self, *, local_connector: str, source: str, source_emote: str, new_name: str | None = None
    ) -> str:
        """`/mirror emote from <source> <source_emote>` - read `source`'s
        emoji and recreate-or-match it *here* on `local_connector`, then link
        the two. `mirror_emote` with the connectors swapped.

        `new_name`, if given, names the local counterpart emoji instead of
        carrying the source emoji's name over (issue #44)."""
        if source not in self._connectors:
            raise LinkError(f"'{source}' isn't a known connector.")
        if source == local_connector:
            raise LinkError("can't mirror an emote from a connector to itself.")
        return await self.mirror_emote(
            local_connector=source, local_emote=source_emote, destination=local_connector, new_name=new_name
        )

    async def list_linked_emotes(
        self, *, local_connector: str, local_emote: str | None = None, service: str | None = None
    ) -> str:
        """Read-only listing, for `/linked emotes` - never raises LinkError.
        With a `local_emote`, shows just that emoji's group; without one (or
        with `service == "all"`), lists every group."""
        if local_emote is not None and (service is None or service.lower() != "all"):
            local_id = await self._resolve_to_id(local_connector, local_emote)
            group_id = await self._emoji_mappings.get_group_id(local_connector, local_id)
            if group_id is None:
                return "This emote isn't linked to any others."
            groups = [await self._emoji_mappings.get_refs(group_id)]
        else:
            all_groups = await self._emoji_mappings.get_all_groups()
            if not all_groups:
                return "No emotes are linked yet."
            groups = list(all_groups.values())

        lines = []
        for refs in groups:
            parts = []
            for ref in sorted(refs, key=lambda r: (r.connector_id, r.emoji_id)):
                info = self._connectors.get(ref.connector_id)
                label = info.label if info else ref.connector_id
                name = await self._resolve_name(ref.connector_id, ref.emoji_id)
                parts.append(f"{label}: {name}" if name == ref.emoji_id else f"{label}: {name} ({ref.emoji_id})")
            lines.append(" ↔ ".join(parts))
        return "Linked emotes:\n" + "\n".join(lines)

    async def unlink_emote(self, *, local_connector: str, local_emote: str, destination: str | None) -> str:
        """`/unlink emote`. `destination` (a connector id) kicks just that one
        member out of the emoji's mapping group; None/"all" (the default)
        dissolves the whole group. A kick that would strand a lone survivor
        dissolves the group instead (a group of one isn't a bridge)."""
        local_id = await self._resolve_to_id(local_connector, local_emote)
        group_id = await self._emoji_mappings.get_group_id(local_connector, local_id)
        if group_id is None:
            raise LinkError("this emote isn't linked to anything.")

        if destination is None or destination.lower() == "all":
            count = await self._emoji_mappings.delete_group(group_id)
            return f"Unlinked this emote's entire mapping group ({count} emote(s) removed)."

        refs = await self._emoji_mappings.get_refs(group_id)
        target = next((r for r in refs if r.connector_id == destination), None)
        if target is None:
            raise LinkError(f"'{destination}' isn't linked in this emote's mapping group.")
        await self._emoji_mappings.delete_ref(destination, target.emoji_id)
        survivors = [r for r in refs if r.connector_id != destination]
        if len(survivors) <= 1:
            await self._emoji_mappings.delete_group(group_id)
        label = self._connectors[destination].label if destination in self._connectors else destination
        return f"Unlinked {label} emote '{target.name}' ({target.emoji_id}) from this mapping group."

    async def _resolve_to_id(self, connector: str, token: str) -> str:
        token = _strip_emote_token(token)
        info = self._connectors.get(connector)
        if info is not None and info.resolve_emoji_id_by_name is not None:
            try:
                emoji_id = await info.resolve_emoji_id_by_name(token)
            except Exception:
                logger.debug("couldn't resolve emoji name %r on %s", token, connector, exc_info=True)
                emoji_id = None
            if emoji_id:
                return emoji_id
        return token

    async def _resolve_name(self, connector_id: str, emoji_id: str) -> str:
        info = self._connectors.get(connector_id)
        if info is None or info.resolve_emoji_name is None:
            return emoji_id
        try:
            name = await info.resolve_emoji_name(emoji_id)
        except Exception:
            logger.debug("couldn't resolve emoji name for %s on %s", emoji_id, connector_id, exc_info=True)
            return emoji_id
        return name or emoji_id


class UserLinker:
    """`/link user` / `/unlink user` / `/linked users` - links a user's
    identity across connectors, for @mention rewriting and masquerade
    override.

    Every id argument also accepts a bare display name / username - resolved
    to an id via the connector's resolve_user_id_by_name hook, falling back
    to treating the token as an id if the hook is absent or comes up empty
    (IRC has no such hook: a user_id there already IS the nick).
    """

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
        source_user_id = await self._resolve_to_id(source, _strip_discord_mention(source_user_id))
        local_user_id = await self._resolve_to_id(local_connector, _strip_discord_mention(local_user_id))
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
            local_user_id = await self._resolve_to_id(local_connector, _strip_discord_mention(local_user_id))
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
        local_user_id = await self._resolve_to_id(local_connector, _strip_discord_mention(local_user_id))
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

    async def _resolve_to_id(self, connector: str, token: str) -> str:
        """A bare display name / username -> its id via the connector's
        resolve_user_id_by_name hook; an absent/raising/empty hook (or an
        already-an-id token) leaves the token untouched."""
        info = self._connectors.get(connector)
        if info is not None and info.resolve_user_id_by_name is not None:
            try:
                user_id = await info.resolve_user_id_by_name(token)
            except Exception:
                logger.debug("couldn't resolve user name %r on %s", token, connector, exc_info=True)
                user_id = None
            if user_id:
                return user_id
        return token

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


class RoleLinker:
    """`/link role` / `/mirror role` / `/unlink role` / `/linked roles` - the
    role-level counterpart of ChannelLinker, modeled on UserLinker (for the
    list/unlink/name-resolution shape) and ChannelLinker.mirror_channel (for
    the ensure-then-link, report-don't-raise-per-destination shape).

    Roles are Discord/Stoat only; IRC has no role concept, so no connector
    there registers any of the role hooks and `/link role` isn't offered.

    Every id argument also accepts a bare role NAME - resolved to an id via
    the connector's resolve_role_id_by_name hook, falling back to treating
    the token as an id if the hook is absent or comes up empty.
    """

    def __init__(self, role_mappings: RoleMappingRepository, connectors: dict[str, ConnectorInfo]) -> None:
        self._role_mappings = role_mappings
        self._connectors = connectors

    @property
    def connectors(self) -> dict[str, ConnectorInfo]:
        return self._connectors

    async def link_role(
        self,
        *,
        local_connector: str,
        local_role: str,
        source: str,
        source_role: str,
        destination_role: str | None = None,
    ) -> str:
        """Link `source`'s `source_role` to `destination_role` (or
        `local_role`) on `local_connector`. Both role arguments accept an id
        or a bare name. Raises LinkError if `source` is unknown, the two are
        the same role, or both are already linked to two different bridge
        groups."""
        if source not in self._connectors:
            raise LinkError(f"'{source}' isn't a known connector.")

        source_id = await self._resolve_to_id(source, source_role)
        local_id = await self._resolve_to_id(local_connector, destination_role or local_role)

        if source == local_connector and source_id == local_id:
            raise LinkError("can't link a role to itself.")

        source_group = await self._role_mappings.get_bridge_group(source, source_id)
        local_group = await self._role_mappings.get_bridge_group(local_connector, local_id)
        if source_group and local_group and source_group != local_group:
            raise LinkError(
                "both roles are already linked, but to different bridge groups - unlink one before relinking."
            )
        bridge_group = source_group or local_group or uuid.uuid4().hex

        source_name = await self._resolve_name(source, source_id)
        local_name = await self._resolve_name(local_connector, local_id)
        await self._role_mappings.upsert(
            RoleMapping(bridge_group=bridge_group, connector_id=source, role_id=source_id, role_name=source_name)
        )
        await self._role_mappings.upsert(
            RoleMapping(
                bridge_group=bridge_group, connector_id=local_connector, role_id=local_id, role_name=local_name
            )
        )

        source_label = self._connectors[source].label
        local_info = self._connectors.get(local_connector)
        local_label = local_info.label if local_info else local_connector
        return (
            f"Linked {source_label} role '{source_name}' ({source_id}) to "
            f"{local_label} role '{local_name}' ({local_id})."
        )

    async def mirror_role(
        self, *, local_connector: str, local_role: str, destination: str, new_name: str | None = None
    ) -> str:
        """Ensure `local_role` (on `local_connector`) has a linked
        counterpart on `destination`: reuses/creates a same-named role there
        via `destination`'s ensure_role() hook, then links it. Reports rather
        than raises for an already-synced pair, a destination that can't
        create roles, or a link conflict - the bulk `mirror_role_all` caller
        shouldn't have one bad destination abort the rest.

        `new_name`, if given, is the name to create/find the counterpart role
        under on `destination` instead of the source role's name (issue #44)."""
        if destination not in self._connectors:
            raise LinkError(f"'{destination}' isn't a known connector.")
        if destination == local_connector:
            raise LinkError("can't mirror a role to its own connector.")

        local_id = await self._resolve_to_id(local_connector, local_role)
        local_name = await self._resolve_name(local_connector, local_id)
        target_name = _clean_new_name(new_name) or local_name

        bridge_group = await self._role_mappings.get_bridge_group(local_connector, local_id)
        if bridge_group is not None:
            existing = await self._role_mappings.get_mapped_roles(bridge_group)
            if any(m.connector_id == destination for m in existing):
                return f"{self._connectors[destination].label}: already synced - skipped."

        dest_info = self._connectors[destination]
        if dest_info.ensure_role is None:
            return f"{dest_info.label}: doesn't support role creation - link it manually with /link role."

        try:
            destination_role_id = await dest_info.ensure_role(target_name)
        except Exception as exc:
            logger.warning("mirror-role: %s.ensure_role(%r) failed: %s", destination, target_name, exc)
            return f"{dest_info.label}: failed to create/find a role: {exc}"

        try:
            return await self.link_role(
                local_connector=destination,
                local_role=destination_role_id,
                source=local_connector,
                source_role=local_id,
            )
        except LinkError as exc:
            return f"{dest_info.label}: {exc}"

    async def mirror_role_all(self, *, local_connector: str, local_role: str) -> str:
        """`/mirror role <local> all` - mirror_role() against every other
        configured connector, one line of summary/skip/error per connector."""
        results = [
            await self.mirror_role(local_connector=local_connector, local_role=local_role, destination=destination)
            for destination in self._connectors
            if destination != local_connector
        ]
        return "\n".join(results) if results else "no other connectors configured."

    async def mirror_role_from(
        self, *, local_connector: str, source: str, source_role: str, new_name: str | None = None
    ) -> str:
        """`/mirror role from <source> <source_role>` - `source`'s role
        already exists; create-or-match a linked counterpart *here* on
        `local_connector` and link them. `mirror_role` with the connectors
        swapped, so bridge-group reuse (via `link_role`) comes for free.

        `new_name`, if given, names the local counterpart role instead of
        carrying the source role's name over (issue #44)."""
        if source not in self._connectors:
            raise LinkError(f"'{source}' isn't a known connector.")
        if source == local_connector:
            raise LinkError("can't mirror a role from a connector to itself.")
        return await self.mirror_role(
            local_connector=source, local_role=source_role, destination=local_connector, new_name=new_name
        )

    async def list_linked_roles(
        self, *, local_connector: str, local_role: str | None = None, service: str | None = None
    ) -> str:
        """Read-only listing, for `/linked roles` - never raises LinkError.
        With a `local_role`, shows just that role's group; without one (or
        with `service == "all"`), lists every group."""
        if local_role is not None and (service is None or service.lower() != "all"):
            local_id = await self._resolve_to_id(local_connector, local_role)
            bridge_group = await self._role_mappings.get_bridge_group(local_connector, local_id)
            if bridge_group is None:
                return "This role isn't linked to any others."
            groups = [await self._role_mappings.get_mapped_roles(bridge_group)]
        else:
            groups_by_id: dict[str, list[RoleMapping]] = {}
            for mapping in await self._role_mappings.get_all():
                groups_by_id.setdefault(mapping.bridge_group, []).append(mapping)
            if not groups_by_id:
                return "No roles are linked yet."
            groups = list(groups_by_id.values())

        lines = []
        for group_mappings in groups:
            parts = []
            for mapping in sorted(group_mappings, key=lambda m: (m.connector_id, m.role_id)):
                info = self._connectors.get(mapping.connector_id)
                label = info.label if info else mapping.connector_id
                name = await self._resolve_name(mapping.connector_id, mapping.role_id)
                parts.append(f"{label}: {name}" if name == mapping.role_id else f"{label}: {name} ({mapping.role_id})")
            lines.append(" ↔ ".join(parts))
        return "Linked roles:\n" + "\n".join(lines)

    async def unlink_role(self, *, local_connector: str, local_role: str, destination: str | None) -> str:
        """`/unlink role`. `destination` (a connector id) kicks just that one
        member out of the role's bridge group; None/"all" (the default)
        dissolves the whole group. A kick that would strand a lone survivor
        dissolves the group instead (a group of one isn't a bridge)."""
        local_id = await self._resolve_to_id(local_connector, local_role)
        bridge_group = await self._role_mappings.get_bridge_group(local_connector, local_id)
        if bridge_group is None:
            raise LinkError("this role isn't linked to anything.")

        if destination is None or destination.lower() == "all":
            count = await self._role_mappings.delete_bridge_group(bridge_group)
            return f"Unlinked this role's entire bridge group ({count} role(s) removed)."

        mapped = await self._role_mappings.get_mapped_roles(bridge_group)
        target = next((m for m in mapped if m.connector_id == destination), None)
        if target is None:
            raise LinkError(f"'{destination}' isn't linked in this role's bridge group.")
        await self._role_mappings.delete_mapping(destination, target.role_id)
        survivors = [m for m in mapped if m.connector_id != destination]
        if len(survivors) <= 1:
            for m in survivors:
                await self._role_mappings.delete_mapping(m.connector_id, m.role_id)
        label = self._connectors[destination].label if destination in self._connectors else destination
        return f"Unlinked {label} role '{target.role_name}' ({target.role_id}) from this bridge group."

    async def _resolve_to_id(self, connector: str, token: str) -> str:
        info = self._connectors.get(connector)
        if info is not None and info.resolve_role_id_by_name is not None:
            try:
                role_id = await info.resolve_role_id_by_name(token)
            except Exception:
                logger.debug("couldn't resolve role name %r on %s", token, connector, exc_info=True)
                role_id = None
            if role_id:
                return role_id
        return token

    async def _resolve_name(self, connector_id: str, role_id: str) -> str:
        info = self._connectors.get(connector_id)
        if info is None or info.resolve_role_name is None:
            return role_id
        try:
            name = await info.resolve_role_name(role_id)
        except Exception:
            logger.debug("couldn't resolve role name for %s on %s", role_id, connector_id, exc_info=True)
            return role_id
        return name or role_id
