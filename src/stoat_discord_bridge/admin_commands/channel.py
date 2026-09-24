"""`ChannelLinker` - `/link channel`, `/mirror channel [to|from|all]`,
`/unlink channel`, `/linked channels`, and Discord thread mirroring."""

from __future__ import annotations

import logging
import re
import uuid
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from stoat_discord_bridge.admin_commands.common import (
    ConnectorInfo,
    LinkedMember,
    LinkError,
    MirrorGuard,
    _clean_new_name,
    _guards_mirror,
    _is_all_token,
    _is_forum_channel,
    _kick_group_member,
    _link_conflict_check,
    _list_entities_for_all,
    _mirror_all_other_connectors,
    _mirror_from_local,
    _mirror_to_destination,
    _refresh_connectors,
    _require_known_connector,
    _resolve_entity_id,
    _resolve_entity_title,
    _run_bulk_mirror,
    collect_linked_members,
    format_linked_listing,
)
from stoat_discord_bridge.channel_structure import clip_name, forum_category_title, thread_category_title
from stoat_discord_bridge.storage.category_mappings import CategoryMappingRepository
from stoat_discord_bridge.storage.channel_mappings import ChannelMapping, ChannelMappingRepository

if TYPE_CHECKING:
    from stoat_discord_bridge.models import StandardMessage

logger = logging.getLogger(__name__)

# A token shaped like a native entity id rather than a human-chosen name: an
# all-digit Discord snowflake, or a 26-char Crockford-base32 ULID (Stoat). Used
# to decide whether an unresolvable `/mirror channel category:` value is a typo'd
# id (reject) or a name for a Category to create (pass through) - see
# ChannelLinker._resolve_destination_category_name.
_BARE_ID_RE = re.compile(r"\A(?:\d{15,}|[0-9A-HJKMNP-TV-Za-hjkmnp-tv-z]{26})\Z")

# `/mirror channel with history` (issue #122): the type of the callback
# ChannelLinker hands off to once its own create/link logic has produced a
# concrete destination channel id - bound to `BridgeCoordinator.backfill_history`
# by bridge.py's run(), since the linker itself has no reference to receivers,
# only ConnectorInfo hooks.
BackfillHistoryHook = Callable[..., Awaitable[str]]

# `with_history`'s default `history_limit` (a bounded, quick sanity-check
# size for the common case of seeding a freshly-linked channel with recent
# context) and the hard ceiling a plain numeric limit is clamped to - only
# the explicit literal `"all"` bypasses this cap for a full-archive backfill
# (issue #122's point 5: `all` must be requested explicitly, never implied
# by an unusually large number).
_DEFAULT_HISTORY_LIMIT = 50
_MAX_HISTORY_LIMIT = 1000


def _resolve_history_limit(raw: int | str | None) -> int | None:
    """`with_history`'s `history_limit` argument, resolved to what
    `ConnectorInfo.fetch_history` expects: `None` (the option wasn't given)
    defaults to `_DEFAULT_HISTORY_LIMIT`; the literal `"all"`
    (case-insensitive) means archive mode - the entire channel history, no
    cap (`fetch_history`'s own `limit=None`); anything else is parsed as a
    positive integer and clamped to `_MAX_HISTORY_LIMIT` as a sanity
    backstop. Raises LinkError for anything that isn't one of those three
    shapes, rather than guessing."""
    if raw is None:
        return _DEFAULT_HISTORY_LIMIT
    if isinstance(raw, str) and raw.strip().lower() == "all":
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise LinkError(f"invalid history limit {raw!r} - use a positive number, or 'all' for the entire history.")
    if value <= 0:
        raise LinkError("history limit must be a positive number, or 'all' for the entire history.")
    return min(value, _MAX_HISTORY_LIMIT)


class ChannelLinker:
    def __init__(
        self,
        channel_mappings: ChannelMappingRepository,
        connectors: dict[str, ConnectorInfo],
        category_mappings: CategoryMappingRepository | None = None,
        guard: MirrorGuard | None = None,
        backfill_history: BackfillHistoryHook | None = None,
    ) -> None:
        # `connectors` is populated in place by bridge.run() as each sender/
        # receiver is constructed - read lazily here, only once a command
        # actually fires, so construction order doesn't matter.
        self._channel_mappings = channel_mappings
        self._connectors = connectors
        # Set by CategoryLinker.__init__ (which is handed this ChannelLinker),
        # so `/link channel` / `/mirror channel` on a Discord forum can hand
        # off to the Category flow (issue #100). None in tests that construct a
        # bare ChannelLinker - a forum then just flat-links as before.
        self._category_linker: "CategoryLinker | None" = None
        # Only `mirror_channel_from` reads this - to resolve the source
        # channel's Category to its already-linked local counterpart. None in
        # tests that don't exercise that path.
        self._category_mappings = category_mappings
        # Shared across every linker by bridge.run(); a lone instance here
        # keeps direct-construction (tests) working - see MirrorGuard.
        self._guard = guard or MirrorGuard()
        # `/mirror channel with history` (issue #122): bound to
        # BridgeCoordinator.backfill_history by bridge.run(). None in tests
        # that don't exercise `with_history` - and mirror_channel raises
        # LinkError if `with_history=True` is requested without it wired.
        self._backfill_history = backfill_history

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
        _skip_forum_redirect: bool = False,
    ) -> str:
        """Link `source`'s `source_id` channel to `destination_id` (or the
        invoking channel, if omitted) on `local_connector`. Returns a
        human-readable summary. Raises LinkError if `source` is unknown, the
        two channels are the same channel, or both are already linked to two
        *different* existing bridge groups (no auto-merge - the operator has
        to unlink one side first).

        `_skip_forum_redirect` is set by `mirror_channel`'s own trailing
        `link_channel` call, which has already decided (per the destination's
        Category support) whether a Discord forum should be Category-linked -
        so this doesn't second-guess it and reject a deliberate flat link."""
        _require_known_connector(self._connectors, source)

        source_id = await self._resolve_to_id(source, source_id)

        explicit_destination = bool(destination_id and destination_id != local_channel_id)
        if not explicit_destination:
            destination_channel_id = local_channel_id
            destination_name = local_channel_name
        else:
            destination_channel_id = await self._resolve_to_id(local_connector, destination_id)
            destination_name = await self._resolve_name(local_connector, destination_channel_id)

        # A Discord forum channel is really a Category (issue #100) - hand a
        # `/link channel` on one off to `/link category`, linking the forum to
        # a Category on the other connector.
        if not _skip_forum_redirect:
            redirect = await self._forum_category_link_redirect(
                source=source,
                source_id=source_id,
                local_connector=local_connector,
                local_id=destination_channel_id,
                explicit_destination=explicit_destination,
            )
            if redirect is not None:
                return redirect

        source_group, destination_group = await _link_conflict_check(
            self._channel_mappings.get_bridge_group,
            source=source,
            source_id=source_id,
            local_connector=local_connector,
            local_id=destination_channel_id,
            self_link_message="can't link a channel to itself.",
            conflict_message=(
                "both channels are already linked, but to different bridge groups - unlink one before relinking."
            ),
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

    @_guards_mirror(_mirror_to_destination)
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
        with_history: bool = False,
        history_limit: int | str | None = None,
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
        already did. `is_thread_category` marks that destination Category as
        thread-only, so `/link-category` later refuses to link it. It's
        passed True by DiscordSenderService._handle_thread_create's
        auto-mirror, and also inferred here whenever `local_channel_id`
        resolves (via the source connector's `resolve_thread_parent` hook) to
        a Discord thread - so a manual `/mirror channel to`/`from` on a thread
        groups it the same way (issue #72).

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
        destination-normalized by `ensure_channel` and matched the same way
        (issue #44).

        `with_history`, if set, backfills the newly-linked destination
        channel with the source channel's message history after a *fresh*
        link succeeds (issue #122) - never on the "already synced - skipped"
        early-return path, so a repeat `/mirror ... with history` on an
        already-linked pair is a natural no-op rather than a duplicate
        backfill. Requires only `local_connector` to support history as a
        *source* (`ConnectorInfo.supports_history`) - the destination needs
        no such support itself, since the backfill just calls its ordinary
        `ReceiverService.receive()` (issue #141: this is what unlocks IRC as
        a `with_history` destination, and, via `IrcSenderService.fetch_history`,
        as a source too). A destination can still opt into an
        extra gate via `ConnectorInfo.supports_history_destination` (IRC
        only: whether its own chanhistory replay is configured, so a
        backfill into a non-chanhistory IRC channel is refused up front
        rather than silently landing as ordinary, non-persistent IRC
        traffic). Also requires a `backfill_history` hook to have been wired
        at construction - raises LinkError otherwise, since silently
        skipping a requested backfill would be surprising. `history_limit` is
        resolved by `_resolve_history_limit` - `None` defaults to
        `_DEFAULT_HISTORY_LIMIT`, the literal `"all"` means the entire
        history, anything else is a positive integer clamped to
        `_MAX_HISTORY_LIMIT`. Not combinable with `local_channel_id == "all"`
        (below) - one backfill request can't fan out across a whole
        connector's channels.

        `local_channel_id == "all"` (case-insensitive, and only that literal
        token - never inferred from an omitted argument) mirrors every
        channel `local_connector` can enumerate via its `list_channels` hook
        to `destination` instead of just one (issue #123) - one line of
        summary/skip/error per channel, paced and capped the same as the
        other bulk helpers in `common.py`. Raises LinkError up front if
        `local_connector` isn't a known connector, has no `list_channels`
        hook (nothing to enumerate with - e.g. IRC), if it can't be listed,
        if there are too many channels to mirror at once, or if `new_name` is
        also given (one name can't apply to every mirrored channel).
        `local_channel_category` / `is_thread_category` /
        `category_from_channel_id` describe a *single* source channel's own
        Category context (set only by the thread-mirror caller, which never
        passes `local_channel_id="all"`), so they're dropped rather than
        forwarded to every enumerated channel; `destination_category`, an
        explicit destination-side override, still applies to all of them."""
        _require_known_connector(self._connectors, destination)
        if destination == local_connector:
            raise LinkError("can't mirror a channel to its own connector.")

        if with_history:
            if self._backfill_history is None:
                raise LinkError("this bridge instance doesn't support 'with history' backfills.")
            local_info = self._connectors[local_connector]
            dest_info = self._connectors[destination]
            if not local_info.supports_history:
                raise LinkError(
                    f"'with history' isn't supported from {local_info.label} - "
                    "it has no channel history to fetch."
                )
            if dest_info.supports_history_destination is not None and not dest_info.supports_history_destination():
                raise LinkError("History is not supported/configured on the target service.")
            # Resolve+validate up front, before any create/link side effects,
            # so an invalid history_limit fails fast rather than after a
            # channel's already been created.
            resolved_history_limit = _resolve_history_limit(history_limit)

        await _refresh_connectors(self._connectors, local_connector, destination)

        if _is_all_token(local_channel_id):
            if _clean_new_name(new_name) is not None:
                raise LinkError("can't use 'all' together with a new name - it would collide across every channel.")
            if with_history:
                raise LinkError(
                    "'with history' can't be combined with local_id 'all' - "
                    "it would trigger a backfill for every enumerated channel."
                )
            _require_known_connector(self._connectors, local_connector)
            info = self._connectors[local_connector]
            entities = await _list_entities_for_all(self._connectors, local_connector, info.list_channels, kind="channel")
            return await _run_bulk_mirror(
                entities,
                lambda cid, cname: self.mirror_channel(
                    local_connector=local_connector,
                    local_channel_id=cid,
                    local_channel_name=cname,
                    destination=destination,
                    destination_category=destination_category,
                ),
            )

        target_name = _clean_new_name(new_name) or local_channel_name

        local_channel_id = await self._resolve_to_id(local_connector, local_channel_id)

        # A Discord forum channel is really a Category - its posts are threads,
        # each already mirrored as its own channel - so `/mirror channel` on one
        # behaves as `/mirror category`: create/link a Category on `destination`
        # and let the forum's posts route into it (issue #100). Only on a plain
        # top-level mirror, and only when `destination` can hold Categories
        # (not IRC - it keeps a flat linked channel per forum post via the
        # thread pipeline, which is the behavior this issue asks it to keep).
        if (
            self._category_linker is not None
            and not is_thread_category
            and category_from_channel_id is None
            and destination_category is None
            and self._connectors[destination].ensure_category is not None
            and await _is_forum_channel(self._connectors, local_connector, local_channel_id)
        ):
            if with_history:
                # A forum redirects to CategoryLinker.mirror_category - there's
                # no single channel here for backfill_history to fetch/relay
                # into, so silently dropping the request would be misleading.
                raise LinkError(
                    "'with history' isn't supported when mirroring a Discord forum channel "
                    "(it's mirrored as a Category, not a single channel)."
                )
            return await self._category_linker.mirror_category(
                local_connector=local_connector,
                local_category_id=local_channel_id,
                local_category_name=local_channel_name,
                destination=destination,
                new_name=new_name,
            )

        if await self._channel_is_hidden(local_connector, local_channel_id):
            raise LinkError(
                f"the bridge bot can't see channel '{local_channel_id}' on "
                f"{self._connectors[local_connector].label} - give it access to that channel first."
            )

        # A manual `/mirror channel` on a Discord thread should behave like the
        # automatic thread-create mirror: land the counterpart under a Category
        # named after the thread's *parent channel* (and let the destination
        # move that parent channel in), not under the parent's own linked
        # Category (issue #72). Detected here rather than in each caller so
        # both `/mirror channel to` (Discord side) and `/mirror channel from`
        # (the other side) pick it up.
        src_info = self._connectors.get(local_connector)
        if not is_thread_category:
            if src_info is not None and src_info.resolve_thread_parent is not None:
                try:
                    thread_parent = await src_info.resolve_thread_parent(local_channel_id)
                except Exception:
                    logger.debug(
                        "resolve_thread_parent(%s) failed on %s", local_channel_id, local_connector, exc_info=True
                    )
                    thread_parent = None
                if thread_parent is not None:
                    is_thread_category = True
                    category_from_channel_id = thread_parent[0]
                    local_channel_category = thread_parent[1]

        bridge_group = await self._channel_mappings.get_bridge_group(local_connector, local_channel_id)
        if bridge_group is not None:
            existing = await self._channel_mappings.get_mapped_channels(bridge_group)
            if any(m.connector_id == destination for m in existing):
                return f"{self._connectors[destination].label}: already synced - skipped."

        dest_info = self._connectors[destination]
        if dest_info.ensure_channel is None:
            return f"{dest_info.label}: doesn't support channel creation - link it manually with /link channel."

        # Normalize (IRC's `#`-prefix + character sterilization; a no-op on
        # every other connector) then clip to the destination's channel-name
        # limit, so the name handed to `ensure_channel` - and the one
        # `link_channel` goes on to store - can't be rejected or silently
        # mangled by the destination's API for being too long (issue #99), and
        # can't disagree with the id `ensure_channel` returns on length.
        # Normalize before clip so sterilization can't push a clipped name back
        # over the limit (issue #51's stored-name/id agreement concern).
        target_name = self._normalize_name(destination, target_name)
        if dest_info.channel_name_limit is not None:
            target_name = clip_name(target_name, dest_info.channel_name_limit)

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

        # A bridge-generated thread group's Category is titled with a marker
        # prefix (thread emoji + `#`) so it stands out from an ordinary
        # same-named Category (issue #98). Applied here - the single point the
        # auto-mirror and both manual-mirror directions funnel through - and
        # before the title reaches `ensure_channel`, whose existing-Category
        # match is by exact title, so an idempotent re-mirror computes the
        # same prefixed string and reuses the Category rather than nesting a
        # `🧵 #🧵 #<name>`. The mirrored *channel* name is left untouched.
        if is_thread_category and category is not None:
            if category_from_channel_id is not None and await _is_forum_channel(
                self._connectors, local_connector, category_from_channel_id
            ):
                # Posts of a Discord forum group under `💬 #<forum>` rather
                # than the ordinary thread group's `🧵 #<parent>` (issue #100).
                category = forum_category_title(category)
            else:
                category = thread_category_title(category)

        # Cosmetic metadata (description / maturity / icon) off the source
        # channel, so the mirrored channel isn't created blank (issue #32).
        # Best-effort - a missing hook or a raising one just means no
        # metadata is carried. Only passed on when there's something to pass:
        # keeps `ensure_channel` callers that don't take the keyword (older
        # test fakes) untouched, and lets each hook apply it on create only.
        metadata = None
        if src_info is not None and src_info.describe_channel is not None:
            try:
                metadata = await src_info.describe_channel(local_channel_id)
            except Exception as exc:
                logger.warning(
                    "mirror channel: %s.describe_channel(%r) failed: %s", local_connector, local_channel_id, exc
                )
        extra = {"metadata": metadata} if metadata is not None else {}

        # Whether the source channel is a voice channel, so the destination
        # creates a voice channel too instead of always defaulting to text
        # (issue #146). Best-effort, same shape as metadata above: a missing
        # hook (e.g. voice_bridging disabled on the source connector) or a
        # raising one just falls through to today's text-channel default -
        # and the kwarg is only added when actually True, so an ensure_channel
        # fake/hook that doesn't accept it is unaffected by a non-voice mirror.
        if src_info is not None and src_info.channel_is_voice is not None:
            try:
                if await src_info.channel_is_voice(local_channel_id):
                    extra["is_voice"] = True
            except Exception as exc:
                logger.warning(
                    "mirror channel: %s.channel_is_voice(%r) failed: %s", local_connector, local_channel_id, exc
                )

        try:
            destination_channel_id = await dest_info.ensure_channel(
                target_name, category, is_thread_category, category_parent_channel_id, **extra
            )
        except Exception as exc:
            logger.warning("mirror channel: %s.ensure_channel(%r) failed: %s", destination, target_name, exc)
            return f"{dest_info.label}: failed to create/find a channel: {exc}"

        try:
            summary = await self.link_channel(
                local_connector=destination,
                local_channel_id=destination_channel_id,
                local_channel_name=target_name,
                source=local_connector,
                source_id=local_channel_id,
                destination_id=None,
                _skip_forum_redirect=True,
            )
        except LinkError as exc:
            return f"{dest_info.label}: {exc}"

        if with_history:
            assert self._backfill_history is not None  # checked above
            try:
                backfill_summary = await self._backfill_history(
                    fetch_history=local_info.fetch_history,
                    source_channel_id=local_channel_id,
                    destination_connector=destination,
                    destination_channel_id=destination_channel_id,
                    limit=resolved_history_limit,
                )
            except Exception as exc:
                # mirror_channel reports rather than raises for every other
                # failure past this point (ensure_channel, link_channel) - the
                # channel was already successfully created and linked here, so
                # a raising backfill_history shouldn't blow up the whole call.
                logger.warning("mirror channel: with_history backfill failed: %s", exc, exc_info=True)
                backfill_summary = f"history backfill failed unexpectedly: {exc}"
            summary = f"{summary} {backfill_summary}"

        return summary

    @_guards_mirror(_mirror_all_other_connectors)
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
        rather than stopping at the first problem. Reserves every destination
        up front, so if any one is mid-`/mirror` the whole fan-out is rejected
        with that connector named, rather than quietly skipping it (issue #79)."""
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

    async def mirror_channel_for_thread(
        self,
        *,
        local_connector: str,
        local_channel_id: str,
        local_channel_name: str,
        destination: str,
        local_channel_category: str | None,
        category_from_channel_id: str,
    ) -> tuple[str, Callable[[], Awaitable[None]] | None]:
        """Discord thread/forum-post auto-mirror's per-destination
        counterpart to `mirror_channel` (issue #124): creates+links the
        destination channel *without* placing it in its thread Category yet,
        so the caller (`DiscordSenderService._handle_thread_create`) can
        relay+pin the thread's starter message before the channel is
        categorized. `ensure_channel`'s contract otherwise bundles create and
        categorize into one call - on Stoat that means a (often slow,
        whole-server-PATCH) category placement always finishes before
        anything gets a chance to post into the new channel, so the starter
        message ends up looking like it was posted into an already-organized
        channel rather than the first thing that happened there.

        Always `is_thread_category=True` and always takes a
        `category_from_channel_id` (the thread's parent channel id) - unlike
        `mirror_channel`, there's no plain non-thread use of this method, so
        the destination-Category-override / linked-Category-lookup /
        thread-parent-auto-detect branches `mirror_channel` needs for its
        general case don't apply here.

        Returns `(summary, finish)` - `summary` matches `mirror_channel`'s
        report-not-raise convention (skip/error still just produce a line of
        text) for a *destination-side* problem - `finish`, present only when
        the channel was actually created-or-matched and linked, is a
        coroutine that completes the deferred category placement (a second
        `ensure_channel` call - a no-op on creation, since the channel now
        already exists by name, so it just runs the category-placement
        branch). Like `mirror_channel`, still *raises* LinkError for an
        unknown `destination` or `destination == local_connector` - a
        caller/programmer error, not something `mirror_channel_all_for_thread`'s
        fan-out (which only ever iterates known, non-local connectors) can
        actually hit."""
        _require_known_connector(self._connectors, destination)
        if destination == local_connector:
            raise LinkError("can't mirror a channel to its own connector.")

        await _refresh_connectors(self._connectors, local_connector, destination)

        target_name = local_channel_name
        local_channel_id = await self._resolve_to_id(local_connector, local_channel_id)

        if await self._channel_is_hidden(local_connector, local_channel_id):
            return (
                f"the bridge bot can't see channel '{local_channel_id}' on "
                f"{self._connectors[local_connector].label} - give it access to that channel first.",
                None,
            )

        bridge_group = await self._channel_mappings.get_bridge_group(local_connector, local_channel_id)
        if bridge_group is not None:
            existing = await self._channel_mappings.get_mapped_channels(bridge_group)
            if any(m.connector_id == destination for m in existing):
                return f"{self._connectors[destination].label}: already synced - skipped.", None

        dest_info = self._connectors[destination]
        if dest_info.ensure_channel is None:
            return (
                f"{dest_info.label}: doesn't support channel creation - link it manually with /link channel.",
                None,
            )

        target_name = self._normalize_name(destination, target_name)
        if dest_info.channel_name_limit is not None:
            target_name = clip_name(target_name, dest_info.channel_name_limit)

        category = local_channel_category
        category_parent_channel_id: str | None = None
        linked_parent = await self._linked_channel(local_connector, category_from_channel_id, destination)
        if linked_parent is not None:
            category_parent_channel_id = linked_parent.channel_id
            if linked_parent.channel_name:
                category = linked_parent.channel_name

        if category is not None:
            if await _is_forum_channel(self._connectors, local_connector, category_from_channel_id):
                category = forum_category_title(category)
            else:
                category = thread_category_title(category)

        src_info = self._connectors.get(local_connector)
        metadata = None
        if src_info is not None and src_info.describe_channel is not None:
            try:
                metadata = await src_info.describe_channel(local_channel_id)
            except Exception as exc:
                logger.warning(
                    "mirror channel: %s.describe_channel(%r) failed: %s", local_connector, local_channel_id, exc
                )
        extra = {"metadata": metadata} if metadata is not None else {}

        try:
            destination_channel_id = await dest_info.ensure_channel(target_name, None, True, None, **extra)
        except Exception as exc:
            logger.warning("mirror channel: %s.ensure_channel(%r) failed: %s", destination, target_name, exc)
            return f"{dest_info.label}: failed to create/find a channel: {exc}", None

        try:
            summary = await self.link_channel(
                local_connector=destination,
                local_channel_id=destination_channel_id,
                local_channel_name=target_name,
                source=local_connector,
                source_id=local_channel_id,
                destination_id=None,
                _skip_forum_redirect=True,
            )
        except LinkError as exc:
            return f"{dest_info.label}: {exc}", None

        async def finish_category_placement() -> None:
            try:
                await dest_info.ensure_channel(
                    target_name, category, True, category_parent_channel_id, **extra
                )
            except Exception as exc:
                logger.warning(
                    "mirror channel: %s.ensure_channel(%r) category placement failed: %s",
                    destination,
                    target_name,
                    exc,
                )

        return summary, finish_category_placement

    @_guards_mirror(_mirror_all_other_connectors)
    async def mirror_channel_all_for_thread(
        self,
        *,
        local_connector: str,
        local_channel_id: str,
        local_channel_name: str,
        local_channel_category: str | None = None,
        category_from_channel_id: str,
    ) -> tuple[str, list[Callable[[], Awaitable[None]]]]:
        """`mirror_channel_for_thread` fanned out to every other configured
        connector - the thread-auto-mirror counterpart to `mirror_channel_all`
        (issue #124). Returns the combined summary plus every destination's
        `finish` callback (skipping destinations that failed/were skipped, so
        `finish is None`) for the caller to await once the starter message has
        been relayed to all of them."""
        results: list[str] = []
        finishers: list[Callable[[], Awaitable[None]]] = []
        for destination in self._connectors:
            if destination == local_connector:
                continue
            summary, finish = await self.mirror_channel_for_thread(
                local_connector=local_connector,
                local_channel_id=local_channel_id,
                local_channel_name=local_channel_name,
                destination=destination,
                local_channel_category=local_channel_category,
                category_from_channel_id=category_from_channel_id,
            )
            results.append(summary)
            if finish is not None:
                finishers.append(finish)
        combined = "\n".join(results) if results else "no other connectors configured."
        return combined, finishers

    @_guards_mirror(_mirror_from_local)
    async def mirror_channel_from(
        self,
        *,
        local_connector: str,
        source: str,
        source_id: str,
        new_name: str | None = None,
        local_category: str | None = None,
        with_history: bool = False,
        history_limit: int | str | None = None,
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
        of carrying the source channel's name over (issue #44).

        `with_history`/`history_limit` are forwarded as-is to `mirror_channel`
        - see its docstring (issue #122), including its rejection of
        `with_history` combined with `source_id == "all"`.

        `source_id == "all"` (case-insensitive) mirrors every channel
        `source` can enumerate instead of just one, via `mirror_channel`'s
        own entity-`all` handling (issue #123) - the literal token is passed
        through unresolved rather than run through `_resolve_to_id`/
        `_resolve_name`, so a source connector that happens to have a real
        channel literally named "all" doesn't narrow the fan-out to just
        that one channel."""
        _require_known_connector(self._connectors, source)
        if source == local_connector:
            raise LinkError("can't mirror a channel from a connector to itself.")

        await _refresh_connectors(self._connectors, source, local_connector)

        if _is_all_token(source_id):
            source_name = source_id
        else:
            source_id = await self._resolve_to_id(source, source_id)
            source_name = await self._resolve_name(source, source_id)

        return await self.mirror_channel(
            local_connector=source,
            local_channel_id=source_id,
            local_channel_name=source_name,
            destination=local_connector,
            destination_category=local_category,
            new_name=new_name,
            with_history=with_history,
            history_limit=history_limit,
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
        category_id = await _resolve_entity_id(
            token, info.resolve_category_id_by_name, connector=connector, kind="category"
        )
        name = await _resolve_entity_title(
            category_id, info.resolve_category_name, connector=connector, kind="category"
        )
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
        linked, or None if the source channel is uncategorized / unresolvable.
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

    async def describe_group(
        self, *, local_connector: str, local_id: str
    ) -> "tuple[str, list[LinkedMember]] | None":
        """The structured counterpart of `list_linked_channels` - the bridge
        group id and every member as a `LinkedMember`, or None if
        `local_id` (an id or bare name, on `local_connector`) isn't linked to
        anything. Used by the Discord in-line link editor (issue #115) to
        build its components off a group's current membership without
        touching `ChannelMappingRepository` directly."""
        local_id = await self._resolve_to_id(local_connector, local_id)
        bridge_group = await self._channel_mappings.get_bridge_group(local_connector, local_id)
        if bridge_group is None:
            return None
        mapped = await self._channel_mappings.get_mapped_channels(bridge_group)
        members = await collect_linked_members(mapped, self._connectors, "channel_id", "channel_name")
        return bridge_group, members

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
        lines = await format_linked_listing(
            mapped,
            self._connectors,
            "channel_id",
            "channel_name",
            marker_for=(local_connector, local_channel_id),
            marker_text=" (this channel)",
        )
        return "Linked channels:\n" + "\n".join(lines)

    async def unlink_channel(self, *, local_connector: str, local_channel_id: str, destination: str | None) -> str:
        """`/unlink channel`. `destination` (a connector id) kicks just that
        one member out of `local_channel_id`'s bridge group - everyone else
        (including this channel) stays linked to each other; None/"all"
        (the default) dissolves the whole group instead, unlinking every
        member. Raises LinkError if the channel isn't linked, or
        `destination` isn't actually a member of its group.

        A literal (case-insensitive) `all` as `local_channel_id` does this
        for every bridge group `local_connector` is in (issue #160) - see
        `_unlink_all_channels`. It's checked before name resolution, the
        same as `/mirror channel ... all`, so a channel literally named `all`
        has to be addressed by id.

        Every channel that ends up with *no* linked counterparts left - the
        kicked one, and any lone survivor a kick strands - is announced to
        its connector via the on_channel_unlinked hook (IRC uses it to post a
        "this channel was unlinked from ..." notice and PART); a channel that
        still has other links stays untouched and unannounced."""
        if _is_all_token(local_channel_id):
            return await self._unlink_all_channels(local_connector, destination)
        local_channel_id = await self._resolve_to_id(local_connector, local_channel_id)
        bridge_group = await self._channel_mappings.get_bridge_group(local_connector, local_channel_id)
        if bridge_group is None:
            raise LinkError("this channel isn't linked to anything.")
        mapped = await self._channel_mappings.get_mapped_channels(bridge_group)

        if destination is None or destination.lower() == "all":
            count = await self._dissolve_group(bridge_group, mapped)
            return f"Unlinked this channel's entire bridge group ({count} channel(s) removed)."

        target = await self._kick_from_group(mapped, destination)
        return (
            f"Unlinked {self._label(destination)} channel '{target.channel_name}' ({target.channel_id}) "
            "from this bridge group."
        )

    async def _unlink_all_channels(self, local_connector: str, destination: str | None) -> str:
        """`/unlink channel all <service|all>` (issue #160). For every bridge
        group `local_connector` has a stored mapping in, kick `destination`'s
        member out (skipping groups without one), or dissolve the group when
        `destination` is `all`. Enumerates stored mappings rather than the
        live channel list, so mappings to since-deleted channels get cleaned
        up too. Unlike the single-channel form, an omitted `destination` is
        rejected instead of meaning "dissolve everything". A group that fails
        is reported as its own line rather than aborting the rest."""
        if destination is None:
            raise LinkError("'all' needs an explicit service - name a connector, or 'all' to dissolve every group.")
        dissolve = _is_all_token(destination)
        local_label = self._label(local_connector)

        # one entry per bridge group, keeping the first local channel seen for display
        local_by_group: dict[str, ChannelMapping] = {}
        for m in await self._channel_mappings.get_all_for_connector(local_connector):
            local_by_group.setdefault(m.bridge_group, m)
        if not local_by_group:
            raise LinkError(f"no channels on {local_label} are linked to anything.")

        lines: list[str] = []
        done = 0
        for bridge_group, local in local_by_group.items():
            prefix = f"'{local.channel_name}'"
            # re-read now, in case a concurrent command changed the group
            mapped = await self._channel_mappings.get_mapped_channels(bridge_group)
            if not dissolve and not any(m.connector_id == destination for m in mapped):
                continue
            try:
                if dissolve:
                    count = await self._dissolve_group(bridge_group, mapped)
                    lines.append(f"{prefix}: dissolved its bridge group ({count} channel(s) removed)")
                else:
                    target = await self._kick_from_group(mapped, destination)
                    lines.append(f"{prefix}: unlinked {self._label(destination)} channel '{target.channel_name}'")
                done += 1
            except Exception as exc:  # report per group, don't abort the rest
                logger.exception("bulk unlink failed for bridge group %s", bridge_group)
                lines.append(f"{prefix}: failed - {exc}")

        if not lines:
            raise LinkError(f"none of {local_label}'s channels are linked to {self._label(destination)}.")
        header = "Dissolved" if dissolve else f"Unlinked {self._label(destination)} from"
        return f"{header} {done} bridge group(s) on {local_label}:\n" + "\n".join(lines)

    async def _dissolve_group(self, bridge_group: str, mapped: list[ChannelMapping]) -> int:
        """Delete every member of `bridge_group` and announce each one.
        Returns how many mappings were removed."""
        count = await self._channel_mappings.delete_bridge_group(bridge_group)
        await self._announce_unlinked(mapped, removed=mapped)
        return count

    async def _kick_from_group(self, mapped: list[ChannelMapping], destination: str) -> ChannelMapping:
        """Kick `destination`'s member out of the group `mapped` describes,
        dissolving a lone survivor, and announce whoever ended up unlinked.
        Returns the kicked mapping."""

        async def _dissolve(survivors: list[ChannelMapping]) -> None:
            # only one member (or none) would be left - a group of one isn't
            # a bridge, so dissolve it fully.
            for m in survivors:
                await self._channel_mappings.delete_mapping(m.connector_id, m.channel_id)

        target, survivors = await _kick_group_member(
            mapped,
            destination,
            id_attr="channel_id",
            not_a_member_message=f"'{destination}' isn't linked in this channel's bridge group.",
            delete_mapping=self._channel_mappings.delete_mapping,
            dissolve_survivors=_dissolve,
        )
        # announce every former member on a dissolve, just the kicked one otherwise
        await self._announce_unlinked(mapped, removed=mapped if len(survivors) <= 1 else [target])
        return target

    def _label(self, connector_id: str) -> str:
        return self._connectors[connector_id].label if connector_id in self._connectors else connector_id

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

    async def _forum_category_link_redirect(
        self,
        *,
        source: str,
        source_id: str,
        local_connector: str,
        local_id: str,
        explicit_destination: bool,
    ) -> str | None:
        """If either side of a `/link channel` resolves to a Discord forum
        channel, link it as a Category via `CategoryLinker.link_category`
        (issue #100) and return that summary; otherwise None. Requires an
        explicit Category on the non-forum side - a forum can't be linked to
        the bare invoking channel."""
        if self._category_linker is None:
            return None
        source_is_forum = await _is_forum_channel(self._connectors, source, source_id)
        local_is_forum = await _is_forum_channel(self._connectors, local_connector, local_id)
        if not source_is_forum and not local_is_forum:
            return None
        if source_is_forum:
            forum_connector, forum_id = source, source_id
            other_connector, other_id = local_connector, local_id
            other_is_explicit = explicit_destination
        else:
            forum_connector, forum_id = local_connector, local_id
            other_connector, other_id = source, source_id
            other_is_explicit = True  # the source side of /link channel is always given
        if not other_is_explicit:
            raise LinkError(
                f"'{forum_id}' is a Discord forum channel - link it to a Category explicitly, "
                f"e.g. `/link channel {forum_connector} {forum_id} <category>`."
            )
        return await self._category_linker.link_category(
            local_connector=other_connector,
            local_category_id=None,
            local_category_name="",
            source=forum_connector,
            source_id=forum_id,
            destination_id=other_id,
        )

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
        `#name`) is returned unchanged."""
        info = self._connectors.get(connector)
        hook = info.resolve_channel_id_by_name if info else None
        return await _resolve_entity_id(token, hook, connector=connector, kind="channel")

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
        hook = info.resolve_channel_name if info else None
        name = await _resolve_entity_title(channel_id, hook, connector=connector_id, kind="channel")
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
