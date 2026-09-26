"""Shared hook/error/parse primitives behind every `/link` `/unlink`
`/mirror` `/linked` admin command - the connector-agnostic `ConnectorInfo`
hook dataclass, `LinkError`/`MirrorInProgressError`, the `MirrorGuard`
serializing concurrent `/mirror` runs, and the id/name-resolution and
conflict-checking helpers every linker in the sibling `channel.py` /
`category.py` / `emote.py` / `user.py` / `role.py` modules shares.

Channels never link automatically - a bridge_group only comes into being via
`ChannelLinker.link_channel`, called directly by `/link channel` or `/mirror
channel`. Categories are the same - only `/link-category` creates a
CategoryLinker bridge_group -
but once a Category *is* linked, a new channel appearing inside it on either
side auto-syncs onto the other's linked Category (CategoryLinker.
sync_new_channel), which is the one place in this package something
auto-links without an explicit admin command.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import logging
from collections.abc import Awaitable, Callable, Iterable, Iterator
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, TypeVar, overload

if TYPE_CHECKING:
    from stoat_discord_bridge.models import (
        ChannelMetadata,
        CustomEmoji,
        EmojiCapacity,
        RoleMetadata,
        StandardMessage,
    )
    from stoat_discord_bridge.services.role_sync import RolePermissionOverride

logger = logging.getLogger(__name__)


def _read_quoted_value(tokens: list[str], i: int, raw: str) -> tuple[str, int]:
    """Finish reading an option value that starts as ``raw`` at ``tokens[i]``.
    A quoted value (``"two words"`` / ``'two words'``) pulls in the following
    tokens up to the closing quote, since callers hand the option parsers a
    list that was already split on whitespace. Returns ``(value, index of the
    last token used)``, with the quotes removed."""
    quote = raw[:1]
    if quote in ('"', "'"):
        raw = raw[1:]
        while not raw.endswith(quote) and i + 1 < len(tokens):
            i += 1
            raw = f"{raw} {tokens[i]}"
        raw = raw[:-1] if raw.endswith(quote) else raw
    return raw, i


def pop_kv_option(tokens: list[str], key: str, *, bare: bool = False) -> tuple[list[str], str | None]:
    """Pull the first ``key:value`` (or ``key=value``) token out of ``tokens``,
    returning ``(remaining tokens, value)`` - ``value`` is ``None`` when no such
    token is present. The key match is case-insensitive; the value keeps its
    original case. This is Stoat's named-option shape (issue #167): Discord
    options are declared by name, so Stoat takes the same name as a
    ``pname:value`` token anywhere in the argument list. (IRC uses
    `pop_flag_option` instead.)

    With ``bare=True`` a lone ``key`` token also counts, as the option given
    with an empty value (`/mirror channel ... history` means "backfill with the
    default limit"). Off by default so an option name can't swallow a channel
    or role that happens to share it.

    A quoted value (``key:"two words"`` / ``key:'two words'``) is reassembled
    and unwrapped (`_read_quoted_value`)."""
    lowered_key = key.lower()
    value: str | None = None
    remaining: list[str] = []
    i = 0
    while i < len(tokens):
        token = tokens[i]
        lowered = token.lower()
        if value is None and lowered[: len(key) + 1] in (f"{lowered_key}:", f"{lowered_key}="):
            value, i = _read_quoted_value(tokens, i, token[len(key) + 1 :])
        elif value is None and bare and lowered == lowered_key:
            value = ""
        else:
            remaining.append(token)
        i += 1
    return remaining, value


@overload
def pop_flag_option(
    tokens: list[str], short: str, long: str, *, takes_value: Literal[True] = ...
) -> tuple[list[str], str | None]: ...


@overload
def pop_flag_option(
    tokens: list[str], short: str, long: str, *, takes_value: Literal[False]
) -> tuple[list[str], bool]: ...


def pop_flag_option(
    tokens: list[str], short: str, long: str, *, takes_value: bool = True
) -> tuple[list[str], str | bool | None]:
    """IRC's named-option shape (issue #167): pull the first ``-<short>
    VALUE`` / ``--<long> VALUE`` / ``--<long>=VALUE`` out of ``tokens``
    wherever it appears, returning ``(remaining tokens, value)``. ``short``
    and ``long`` are given without dashes; the flag match is
    case-insensitive (IRC commands are typed in any case) and the value keeps
    its case. ``value`` is ``None`` when the flag is absent, and ``""`` when it
    is the last token with nothing after it. Quoted values work as in
    `pop_kv_option`.

    With ``takes_value=False`` the flag stands alone and ``value`` is just
    whether it was present."""
    flags = {f"-{short.lower()}", f"--{long.lower()}"}
    long_eq = f"--{long.lower()}="
    found = False
    value: str | None = None
    remaining: list[str] = []
    i = 0
    while i < len(tokens):
        token = tokens[i]
        lowered = token.lower()
        if not found and lowered in flags:
            found = True
            if takes_value:
                if i + 1 < len(tokens):
                    value, i = _read_quoted_value(tokens, i + 1, tokens[i + 1])
                else:
                    value = ""
        elif not found and takes_value and lowered.startswith(long_eq):
            found = True
            value, i = _read_quoted_value(tokens, i, token[len(long_eq) :])
        else:
            remaining.append(token)
        i += 1
    if not takes_value:
        return remaining, found
    return remaining, value


def _clean_new_name(raw: str | None) -> str | None:
    """A `new_name` override off a `/mirror <noun> to|from` command, trimmed -
    or None if it was blank/absent, meaning "carry the source name over" (the
    historical behavior). Never normalized or length-checked here: the shared
    linker clips it to the destination's `ConnectorInfo.<entity>_name_limit`
    (issue #99), and each connector's `ensure_*` hook destination-normalizes
    whatever name it's handed after that (IRC's `#channel` sterilizing, an
    emoji-name reject, ...) - so routing the override through the linker + hook
    is what makes it "destination-normalized", and the same call still
    get-or-creates so a same-named existing entity is matched rather than
    duplicated (issue #44)."""
    if raw is None:
        return None
    stripped = raw.strip()
    return stripped or None


async def _refresh_connectors(connectors: "dict[str, ConnectorInfo]", *connector_ids: str) -> None:
    """Best-effort force-refresh of each named connector's cached server
    state before a `/mirror` command resolves names or runs a get-or-create
    against it, so an entity created since the gateway connected isn't missed
    and duplicated (issue #81, see `ConnectorInfo.refresh`).

    A `None` connector id, a duplicate, a connector with no `refresh` hook, or
    a hook that raises are all skipped silently. The hooks throttle repeat
    calls themselves, so passing the same id again (a `/mirror ... all` /
    `/mirror category` fan-out re-entering a per-destination mirror) is cheap.
    """
    seen: set[str] = set()
    for connector_id in connector_ids:
        if not connector_id or connector_id in seen:
            continue
        seen.add(connector_id)
        info = connectors.get(connector_id)
        if info is None or info.refresh is None:
            continue
        try:
            await info.refresh()
        except Exception:
            logger.debug("refresh() failed on %s before a /mirror command", connector_id, exc_info=True)


class LinkError(Exception):
    """User-facing error - callers should relay str(exc) back to the admin who ran the command."""


class NothingLinkedError(LinkError):
    """A bulk `/unlink <noun> all` that found nothing to unlink - the
    connector has no links of that kind, or none to the named service.
    `/unlink all` (issue #181) skips a kind that raises this instead of
    reporting it as a failure."""


class MirrorInProgressError(LinkError):
    """A `/mirror <x>` (or `/import` / `/export`, issue #161) command whose
    connector is still being written to by another such run - rejected up front rather than
    left to race the first one into duplicate channels/Categories/roles/
    emoji (issue #79). A LinkError subclass, so every existing
    `except LinkError` / "relay str(exc) to the admin" path handles it."""


class MirrorGuard:
    """Serializes `/mirror <x> to|from|all` runs by *destination connector*
    so two of them can't write into the same service at once and duplicate
    each other's work (issue #79 - `/mirror channel` especially is slow, and
    a second one firing mid-run re-does the not-yet-linked channels). One
    instance is shared by every linker (ChannelLinker / CategoryLinker /
    EmoteLinker / RoleLinker), so `/mirror channel to stoat` and
    `/mirror role to stoat` exclude each other too.

    A reservation is keyed to the running asyncio task: one mirror operation
    that fans out across several linker methods in the same task (e.g.
    `/mirror category`, which also mirrors each child channel, or the
    `... all` variants) re-enters its own reservation freely, while a
    genuinely concurrent command - always a separate task - is rejected with
    a user-facing MirrorInProgressError. `reserve` never awaits between
    checking and claiming, so it's atomic on the single event loop."""

    def __init__(self) -> None:
        # destination connector id -> the asyncio task holding it
        self._held: dict[str, object] = {}

    @contextlib.contextmanager
    def reserve(
        self, destinations: Iterable[str], connectors: dict[str, ConnectorInfo]
    ) -> Iterator[None]:
        # The owner identity that makes a reservation reentrant: the running
        # task, or - if there somehow isn't one - a fresh object, so the call
        # fails *closed* (every held destination reads as a clash) rather than
        # matching a stored None and silently skipping the guard.
        owner: object = asyncio.current_task() or object()
        wanted = [d for d in dict.fromkeys(destinations) if d]
        clash = [d for d in wanted if d in self._held and self._held[d] is not owner]
        if clash:
            names = ", ".join(
                sorted((connectors[d].label if d in connectors else d) for d in clash)
            )
            raise MirrorInProgressError(
                f"another mirror/import/export into {names} is still running - wait for it to finish "
                "before starting another, or its results may be duplicated."
            )
        claimed = [d for d in wanted if d not in self._held]
        for d in claimed:
            self._held[d] = owner
        try:
            yield
        finally:
            for d in claimed:
                self._held.pop(d, None)


_MirrorResult = TypeVar("_MirrorResult")


def _guards_mirror(
    destinations: Callable[[object, dict[str, object]], Iterable[str]],
) -> Callable[[Callable[..., Awaitable[_MirrorResult]]], Callable[..., Awaitable[_MirrorResult]]]:
    """Decorator for the linker `mirror_*` entry points: hold a `self._guard`
    reservation on the destination connector(s) for the whole call, so a
    second concurrent `/mirror` into the same service is rejected up front
    (issue #79). `destinations(self, kwargs)` returns the connector ids to
    reserve. All decorated methods take only keyword args after `self`, but
    the wrapper stays fully transparent (`*args, **kwargs`); nested calls
    within one asyncio task re-enter the reservation freely (see MirrorGuard).
    The `... all` fan-outs reserve *every* destination up front
    (`_mirror_all_other_connectors`), so if any one is busy the whole
    operation is rejected before it starts rather than silently dropping that
    connector - the error names which one. Most decorated methods return a
    plain summary `str`; `ChannelLinker.mirror_channel_all_for_thread` (issue
    #124) instead returns `(str, list[finish-callback])`, hence the generic
    `_MirrorResult` rather than a hardcoded `str`."""

    def deco(fn: Callable[..., Awaitable[_MirrorResult]]) -> Callable[..., Awaitable[_MirrorResult]]:
        @functools.wraps(fn)
        async def wrapper(*args: object, **kwargs: object) -> _MirrorResult:
            self = args[0]
            guard: MirrorGuard = self._guard  # type: ignore[attr-defined]
            connectors = self._connectors  # type: ignore[attr-defined]
            with guard.reserve(destinations(self, kwargs), connectors):
                return await fn(*args, **kwargs)

        return wrapper

    return deco


def _mirror_to_destination(self: object, kw: dict[str, object]) -> Iterable[str]:
    """`/mirror <x> to <service>` - reserve just the named destination."""
    return (kw["destination"],)  # type: ignore[return-value]


def _mirror_from_local(self: object, kw: dict[str, object]) -> Iterable[str]:
    """`/mirror <x> from <service> <id>` - the counterpart is created on the
    invoking connector, so that's the one to reserve."""
    return (kw["local_connector"],)  # type: ignore[return-value]


def _mirror_all_other_connectors(self: object, kw: dict[str, object]) -> Iterable[str]:
    """`/mirror <x> all` - reserve every connector the fan-out will write to,
    so a single busy destination rejects the whole operation up front (with
    that connector named) rather than being quietly skipped."""
    return [d for d in self._connectors if d != kw["local_connector"]]  # type: ignore[attr-defined]


def _transfer_both_connectors(self: object, kw: dict[str, object]) -> Iterable[str]:
    """`/import` / `/export` (issue #161) - reserve both connectors involved,
    so a transfer and a `/mirror` touching either one exclude each other.
    `MirrorGuard.reserve` collapses a same-connector transfer to one."""
    return (kw["local_connector"], kw["service"])  # type: ignore[return-value]


# --------------------------------------------------------------------------
# Entity-level `all` (issue #123): `/mirror <noun> to <service> all` mirrors
# every local entity of that kind to `<service>`, and `/mirror <noun> from
# <service> all` pulls in every entity of that kind from `<service>` - the
# orthogonal axis to the existing destination-level `all` above (which picks
# every *destination* for one named entity). Both axes are independent, so
# `/mirror channel to all all` (every local channel, to every connector)
# reaches this fan-out once per destination via the existing destination-`all`
# loop, with no extra code needed here.

# Small delay between each entity in a bulk fan-out, so a large `all` doesn't
# front-load an unpaced burst of channel/category/role/emote-create calls
# against a platform's rate limiting (the "need to respect limits for
# requests" ask in issue #123) - a real concern here since, unlike the
# destination axis (bounded by the number of configured connectors), the
# entity axis is bounded by how many channels/categories/roles/emoji exist on
# a connector, which is not small.
_BULK_ENTITY_PACING_SECONDS: float = 0.25

# `_run_bulk_mirror` groups entities into batches of this size (issue #157) -
# every `_BULK_ENTITY_BATCH_SIZE`-th entity takes the longer
# `_BULK_ENTITY_BATCH_PACING_SECONDS` pause instead of the normal per-entity
# one, easing off further before a platform's stricter creation-specific rate
# limits (and, for a Discord slash command, the 15-minute
# interaction-followup token window) come into play on a very large `all`.
# Entities are no longer capped outright above this size - see
# `_list_entities_for_all`.
_BULK_ENTITY_BATCH_SIZE: int = 50
_BULK_ENTITY_BATCH_PACING_SECONDS: float = 5.0


def _is_all_token(token: str | None) -> bool:
    """Whether `token` is the literal (case-insensitive) `all` marking an
    entity-level `/mirror <noun> to|from ... all` (issue #123). `None` - the
    argument was omitted - is never `all`: omission must keep its existing,
    narrower meaning (the invoking channel/Category, or "no role/emote
    picked yet") in every caller, never silently expand into the expensive
    bulk branch (see point 3 of the issue's plan)."""
    return token is not None and token.lower() == "all"


async def _list_entities_for_all(
    connectors: "dict[str, ConnectorInfo]",
    connector_id: str,
    list_hook: Callable[[], Awaitable[list[tuple[str, str]]]] | None,
    *,
    kind: str,
) -> list[tuple[str, str]]:
    """Every `(id, name)` pair `list_hook` reports for `connector_id`, to
    drive an entity-level `all` mirror. Unlike the best-effort autocomplete
    callers of the same `list_*` hooks (which treat a missing hook or a
    raised exception as "no suggestions"), a bulk mirror has no such
    fallback - a connector with no `list_*` hook for `kind` (e.g. IRC for
    anything but channels) genuinely can't enumerate "every entity", so this
    raises a user-facing LinkError instead of silently doing nothing. No
    longer caps the result (issue #157) - `_run_bulk_mirror` batches an
    oversized list instead of this rejecting it up front."""
    info = connectors.get(connector_id)
    label = info.label if info else connector_id
    if list_hook is None:
        raise LinkError(f"{label} doesn't support listing {kind}s - can't use 'all' here.")
    try:
        entities = await list_hook()
    except Exception as exc:
        raise LinkError(f"{label}: couldn't list {kind}s: {exc}") from exc
    return entities


async def _run_bulk_mirror(
    entities: list[tuple[str, str]],
    mirror_one: Callable[[str, str], Awaitable[str]],
    *,
    pacing_seconds: float = _BULK_ENTITY_PACING_SECONDS,
    batch_size: int = _BULK_ENTITY_BATCH_SIZE,
    batch_pacing_seconds: float = _BULK_ENTITY_BATCH_PACING_SECONDS,
) -> str:
    """Run `mirror_one(entity_id, entity_name)` for every entity in
    `entities`, in order, pacing each call `pacing_seconds` apart (not before
    the first) so a large fan-out doesn't front-load a burst of creates.
    Every `batch_size`-th entity (issue #157) takes the longer
    `batch_pacing_seconds` pause instead of the normal one, easing off
    further before a platform's stricter creation-specific rate limits come
    into play on a very large `all`. Joins the per-entity result lines the
    same way the existing destination-`all` fan-outs (`mirror_channel_all`
    and friends) do - any exception from one entity (a `LinkError`, or a
    genuinely unexpected one that slipped past `mirror_one`'s own
    per-connector-problem handling) is caught and reported as its own line
    (`'<name>': <message>`) rather than aborting the rest, matching those
    methods' "report, don't raise, per problem" convention."""
    if not entities:
        return "nothing to mirror - no entities found."
    lines: list[str] = []
    for i, (entity_id, entity_name) in enumerate(entities):
        if i:
            await asyncio.sleep(batch_pacing_seconds if i % batch_size == 0 else pacing_seconds)
        try:
            lines.append(await mirror_one(entity_id, entity_name))
        except Exception as exc:
            if not isinstance(exc, LinkError):
                logger.warning("bulk mirror: entity %r failed: %s", entity_name, exc)
            lines.append(f"'{entity_name}': {exc}")
    return "\n".join(lines)


@dataclass(frozen=True)
class ConnectorInfo:
    id: str
    label: str
    # Maximum length this connector accepts for a channel / category / role
    # name. `/mirror channel|category|role` (every direction, `new_name`
    # override included) clips the name it hands the destination's `ensure_*`
    # hook to the matching limit via `channel_structure.clip_name`, so a name
    # that fits the source platform is trimmed cleanly by the bridge rather
    # than rejected or silently mangled by the destination's API (issue #99).
    # `None` means "no bridge-side clip" - leave length handling to the
    # platform. One per entity kind so a future platform with asymmetric caps
    # is expressible. Discord is 100 across the board; Stoat 32; IRC's
    # `channel_name_limit` is a conservative RFC-derived default (its live
    # server-advertised CHANNELLEN, tighter on some networks, is applied as a
    # backstop in `IrcSenderService.ensure_channel`), and it has no category
    # or role concept.
    channel_name_limit: int | None = None
    category_name_limit: int | None = None
    role_name_limit: int | None = None
    # This connector's own bridge-bot user id, if known - wired lazily (a
    # lambda) in bridge.py since the underlying client may not be connected
    # yet when connectors are wired. Used only by
    # `BotWhitelistManager.whitelist_bot` as a guardrail against whitelisting
    # the bridge's own bot (issue #120); the sender-side `webhook_id` /
    # `self._self_id` echo checks are the real loop protection, and run
    # unconditionally before any whitelist check regardless of this field.
    self_user_id: Callable[[], str | None] | None = None
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
    # the Category a channel sits in, or None if it's uncategorized / can't be
    # resolved. Used by `/mirror channel from <service> <external_id>` to place
    # the freshly-created local channel into the local counterpart of the
    # source channel's linked Category (rather than a fresh same-named one).
    # IRC leaves this unset - it has no Category concept.
    resolve_channel_category: Callable[[str], Awaitable[tuple[str, str] | None]] | None = None
    # Best-effort "is this native channel id a thread, and if so what's its
    # parent channel?" lookup -> (parent_channel_id, parent_channel_name), or
    # None if the channel isn't a thread / can't be resolved. Only Discord
    # wires this (threads are a Discord-only concept). `mirror_channel` uses
    # it so a manual `/mirror channel to`/`from` on a Discord thread groups
    # the counterpart under a Category named after the thread's parent
    # channel - and moves that parent channel into it - exactly like the
    # automatic thread-create mirror does, rather than dropping the thread
    # into the parent's own linked Category (issue #72).
    resolve_thread_parent: Callable[[str], Awaitable[tuple[str, str] | None]] | None = None
    # Best-effort "is this native channel id a Discord forum/media channel?"
    # check -> True (it's a forum), False (it isn't), or None (can't tell -
    # bad id, uncached, an error, or a connector kind with no forum concept).
    # Only Discord wires this. `ChannelLinker.link_channel` / `mirror_channel`
    # redirect a forum target into the Category flow (`CategoryLinker`) - a
    # forum acts like a Category, its posts are threads each already mirrored
    # as its own channel (issue #100). `CategoryLinker` also consults it to
    # title a forum-sourced Category `💬 #<forum>` rather than `🧵 #<parent>`.
    is_forum_channel: Callable[[str], Awaitable[bool | None]] | None = None
    # Best-effort "is this native channel id a voice channel?" check -> True,
    # False, or None (can't tell - bad id, uncached, an error, or a connector
    # kind with no voice concept). Discord/Stoat implement it; IRC leaves it
    # unset (no voice channels), as does a Discord/Stoat connector with
    # `voice_bridging: false` in config.yaml (issue #113) - either way, a
    # channel this hook can't answer for never counts toward a bridge group's
    # voice-bridgeable membership. Used by `services/voice/coordinator.py`'s
    # `VoiceBridgeCoordinator` to classify which linked channel groups are
    # voice-bridgeable (>= 2 voice channels on voice-capable connectors).
    channel_is_voice: Callable[[str], Awaitable[bool | None]] | None = None
    # Best-effort snapshot of the non-bot user ids currently present in voice
    # channel `channel_id`, or None if it can't be determined right now (bad
    # id, uncached, an error) - callers should leave whatever presence they
    # already had for this connector/channel alone on a None rather than
    # treat it as "now empty". Only meaningful alongside `channel_is_voice`;
    # IRC and a non-voice-bridging connector leave this unset too. Used by
    # `VoiceBridgeCoordinator.refresh_groups` for startup seeding and to
    # self-heal drift between the coordinator's presence table (fed live by
    # each sender's voice-state events) and the platform's own state.
    voice_occupants: Callable[[str], Awaitable["set[str] | None"]] | None = None
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
    # Category (if any) as one that groups a Discord thread/forum-post with
    # its siblings (see DiscordSenderService._handle_thread_create), via
    # CategoryLinker.bind_thread_category - so `/link-category` later
    # refuses to link it. Passed True by the thread auto-mirror and also
    # inferred by ChannelLinker.mirror_channel whenever the source channel
    # is a Discord thread (issue #72); False for a plain channel mirror and
    # CategoryLinker.sync_new_channel's own auto-sync.
    # The fourth argument, category_parent_channel_id, is this connector's
    # own channel id for the thread's parent channel (set alongside
    # is_thread_category). It keys the persistent parent->thread-Category
    # binding (ThreadCategoryRepository), so the Category is resolved by id
    # rather than by title on later threads - surviving a Category rename.
    # None for every other caller.
    # An optional `metadata` keyword (a `ChannelMetadata`) is passed by
    # `/mirror channel` when the source channel had any - the hook applies
    # it only when it actually creates the channel, never onto a reused one.
    # An optional `is_voice` keyword (bool, only ever passed as True - never
    # explicitly False) is passed by `/mirror channel` when the source
    # channel's own `channel_is_voice` hook reports it as a voice channel
    # (issue #146) - the hook should match/create a voice-capable channel
    # instead of a text one. Omitted entirely on a text-channel mirror, so a
    # hook/test fake that doesn't accept the keyword at all is unaffected.
    ensure_channel: Callable[..., Awaitable[str]] | None = None
    # Best-effort channel-history fetch, already converted to
    # `StandardMessage` and always returned oldest-first (so relaying the
    # list in order reproduces the original chronology), for `/mirror
    # channel with history` (issue #122): `fetch_history(channel_id, limit)` -
    # `limit=None` means "the entire channel history", oldest message first
    # (archive mode's `limit:all`); a numeric `limit` instead means
    # approximately the `limit` *most recent* messages (still returned
    # oldest-first). Discord and Stoat always wire this; IRC wires it too as
    # of issue #141, via `IrcSenderService.fetch_history` - a PART+re-JOIN
    # that captures whatever chanhistory-replay burst follows, resolving to
    # an empty list (the same "nothing to backfill" outcome every connector
    # produces in the no-history case) if the target network/channel has no
    # chanhistory module enabled at all. `ChannelLinker.mirror_channel` only
    # requires the *source* to wire this before allowing `with_history` -
    # the destination needs no such support (its ordinary `receive()` is all
    # a backfill uses); see `supports_history_destination` below for the one
    # extra destination-side gate that does exist. Best-effort in the same
    # sense as the other hooks: an unresolvable channel or a raising fetch is
    # the caller's problem to report, not something this hook itself needs
    # to swallow. `/import` / `/export` (issue #161) also pass
    # `include_relayed=True` to keep posts the bridge relayed in and bot
    # posts, which `/mirror ... with history` skips.
    fetch_history: Callable[..., Awaitable[list["StandardMessage"]]] | None = None
    # Best-effort, synchronous "would history backfilled *into* this
    # connector's channel actually persist?" check - None (every connector
    # but IRC) means "no extra restriction, always fine" (a Discord/Stoat
    # channel always keeps whatever's relayed into it). Only IRC wires this
    # (issue #141's IRC -> IRC decision): a `with_history` mirror *into* an
    # IRC channel is only meaningful if this connector's own chanhistory
    # replay module is configured (`default_channel_modes`'s `H` flag) -
    # otherwise the backfilled messages would have no more persistence than
    # any other live IRC message there, so `ChannelLinker.mirror_channel`
    # refuses up front rather than silently landing a "successful" backfill
    # nothing will ever replay back. A config-level check (not a live
    # per-channel MODE query), matching this feature's existing "gold setup"
    # scoping for chanhistory detection.
    supports_history_destination: Callable[[], bool] | None = None
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
    # then reports that connector as unsupported (mirrors create_role). None
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
    # Always creates a new role named `name` on this connector, returning its
    # native id - never matches an existing one. `/mirror role` looks for a
    # same-named role itself via resolve_role_id_by_name first, so it can
    # refuse a match that's already linked elsewhere (issue #183). None if
    # this connector kind can't create roles - `/mirror role` then reports
    # that connector as unsupported.
    # An optional `metadata` keyword (a `RoleMetadata`) is passed by
    # `/mirror role` when the source role had any (issue #179).
    create_role: Callable[..., Awaitable[str]] | None = None
    # Best-effort read of a role's cosmetic metadata (color, hoist) as a
    # `RoleMetadata`, or None if the role can't be resolved. `/mirror role`
    # reads this off the *source* role and hands it to the destination's
    # `create_role` (issue #179) - the role counterpart of describe_channel.
    describe_role: Callable[[str], Awaitable["RoleMetadata | None"]] | None = None
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
    # Best-effort remaining emoji-slot capacity on this connector (issue
    # #157), so `/mirror emote` can skip a doomed create before spending an
    # image download and API call on it. None - hook unset, or it raises or
    # returns None - means "capacity unknown," which callers treat as
    # "attempt the create and let it succeed or fail normally." Discord-only:
    # Guild.emoji_limit + the cached emoji list give this for free with no
    # extra request; stoat.py's client has no equivalent queryable limit (only
    # a TooManyEmoji error at creation time), so Stoat leaves this unset.
    emoji_capacity: Callable[[], Awaitable["EmojiCapacity | None"]] | None = None
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

    # Best-effort "re-fetch this connector's cached server state from the API"
    # - channels, categories, roles, custom emoji, members. Every `/mirror`
    # command (`/mirror channel|category|role|emote`, all three of
    # `to`/`from`/`all`) calls it on both the source and destination connector
    # before resolving any name/id or running a get-or-create, so an entity
    # created since the gateway connected - which the cache-only
    # `resolve_*` / `ensure_*` / `list_*` reads would otherwise miss, then
    # duplicate - is seen (issue #81). Only Stoat wires it: stoat.py 1.2.1
    # populates the cached Server once at connect and refreshes it from only a
    # narrow set of gateway events (Categories never - see issue #66), so its
    # cache genuinely drifts. Discord's cache is kept live by gateway events
    # and IRC's channel/user state is already live, so both leave this unset.
    # Best-effort: a missing hook, a raising one, or a partial refresh are all
    # tolerated - the reads that follow still fall back to whatever the cache
    # holds. The hook rate-limits itself, so the bulk `all` / `/mirror
    # category` fan-out that re-enters a mirror per destination/child stays
    # one network round-trip.
    refresh: Callable[[], Awaitable[None]] | None = None

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

    @property
    def supports_history(self) -> bool:
        """Whether this connector can be a `/mirror channel with history`
        *source* - whichever connector kinds wire `fetch_history` (Discord,
        Stoat, and - since issue #141 - IRC). `ChannelLinker.mirror_channel`
        only checks this on the source; a destination needs no such support
        of its own (see `supports_history_destination` for IRC's one extra
        destination-side gate)."""
        return self.fetch_history is not None


# The id<->name walk shared by every `<Kind>Linker._resolve_to_id` /
# `_resolve_name` (issue #106 - CategoryLinker's own
# _resolve_category_id/_resolve_category_title already did this for
# Categories alone; this is the generalized form every linker delegates to).
async def _resolve_entity_id(
    token: str,
    id_by_name_hook: Callable[[str], Awaitable[str | None]] | None,
    *,
    connector: str,
    kind: str,
) -> str:
    """Resolve a bare `kind` name `token` to its native id via
    `id_by_name_hook`, falling back to `token` unchanged - already an id, no
    such hook on this connector (e.g. IRC, whose ids already ARE the
    human-typed name), or the hook raised/came up empty."""
    if id_by_name_hook is None:
        return token
    try:
        resolved = await id_by_name_hook(token)
    except Exception:
        logger.debug("couldn't resolve %s name %r on %s", kind, token, connector, exc_info=True)
        return token
    return resolved or token


async def _resolve_entity_title(
    entity_id: str,
    name_hook: Callable[[str], Awaitable[str | None]] | None,
    *,
    connector: str,
    kind: str,
) -> str | None:
    """Resolve a native `kind` id to its display name/title via `name_hook`,
    or None - no such hook, it raised, or it came up empty. Callers fall back
    to the id itself when this returns None."""
    if name_hook is None:
        return None
    try:
        name = await name_hook(entity_id)
    except Exception:
        logger.debug("couldn't resolve %s id %r on %s", kind, entity_id, connector, exc_info=True)
        return None
    return name or None


async def _is_forum_channel(
    connectors: "dict[str, ConnectorInfo]", connector_id: str, channel_id: str
) -> bool:
    """True only when `connector_id`'s `is_forum_channel` hook says, for
    certain, that `channel_id` is a Discord forum/media channel. A missing
    hook (every non-Discord connector), an error, or a None ("can't tell")
    all return False - so a forum redirect only ever fires on a definite
    yes (issue #100)."""
    info = connectors.get(connector_id)
    if info is None or info.is_forum_channel is None:
        return False
    try:
        return await info.is_forum_channel(channel_id) is True
    except Exception:
        logger.debug("is_forum_channel(%s) failed on %s", channel_id, connector_id, exc_info=True)
        return False


def _require_known_connector(connectors: "dict[str, ConnectorInfo]", connector_id: str) -> None:
    """The `if <connector> not in self._connectors: raise LinkError(...)`
    guard every `link_*`/`mirror_*`/`mirror_*_from` entry point opens with."""
    if connector_id not in connectors:
        raise LinkError(f"'{connector_id}' isn't a known connector.")


def _reject_self_link(*, source: str, source_id: str, local_connector: str, local_id: str, message: str) -> None:
    """The `if source == local_connector and source_id == local_id: raise
    LinkError(...)` guard every linker's `link_<kind>` opens its
    conflict-checking with. Split out from `_group_conflict_check` (rather
    than folded into one combined step, as `_link_conflict_check` below
    does for the common case) so `CategoryLinker.link_category` can still
    run its thread-category guard in between the two, matching its original
    ordering."""
    if source == local_connector and source_id == local_id:
        raise LinkError(message)


async def _group_conflict_check(
    get_group: Callable[[str, str], Awaitable[str | None]],
    *,
    source: str,
    source_id: str,
    local_connector: str,
    local_id: str,
    conflict_message: str,
) -> tuple[str | None, str | None]:
    """Look up each side's existing group via `get_group` and raise LinkError
    (`conflict_message`) if both belong to two *different* ones already (no
    auto-merge - the operator unlinks one side first). Returns `(source_group,
    local_group)` rather than a single merged id, since EmoteLinker's
    reservation-based persistence branches on which specific side is already
    grouped - most callers just combine them themselves
    (`source_group or local_group or uuid.uuid4().hex`)."""
    source_group = await get_group(source, source_id)
    local_group = await get_group(local_connector, local_id)
    if source_group and local_group and source_group != local_group:
        raise LinkError(conflict_message)
    return source_group, local_group


async def _link_conflict_check(
    get_group: Callable[[str, str], Awaitable[str | None]],
    *,
    source: str,
    source_id: str,
    local_connector: str,
    local_id: str,
    self_link_message: str,
    conflict_message: str,
) -> tuple[str | None, str | None]:
    """The shared tail of every linker's `link_<kind>` (`CategoryLinker`
    excepted - see `_reject_self_link`'s docstring), once both sides have
    been resolved to native ids: refuse linking an entity to itself, then
    apply `_group_conflict_check`."""
    _reject_self_link(source=source, source_id=source_id, local_connector=local_connector, local_id=local_id, message=self_link_message)
    return await _group_conflict_check(
        get_group,
        source=source,
        source_id=source_id,
        local_connector=local_connector,
        local_id=local_id,
        conflict_message=conflict_message,
    )


@dataclass(frozen=True)
class LinkedMember:
    """One connector's side of a bridge/link/mapping group - the structured
    counterpart of the `"{label}: {name} ({id})"` strings `format_linked_listing`
    renders, for a caller (the Discord in-line link editor - issue #115) that
    needs to build UI components off a group's membership rather than just
    print it. `connector_id` is the raw config id (`ChannelMapping.connector_id`
    etc.); `label` is that connector's display label, already resolved so a
    caller never has to re-look it up."""

    connector_id: str
    label: str
    entity_id: str
    name: str


async def collect_linked_members(
    mappings: "Iterable[object]",
    connectors: "dict[str, ConnectorInfo]",
    id_attr: str,
    name_attr: str | None = None,
    *,
    resolve_name: Callable[[str, str], Awaitable[str]] | None = None,
) -> list[LinkedMember]:
    """The mapping-to-member gather step shared by every `list_linked_*`
    method and (issue #115) `describe_group`: one `LinkedMember` per mapping,
    sorted by `(connector_id, <id_attr>)`. `format_linked_listing` is just
    this plus the display-string join.

    `name_attr` reads the display name straight off the stored mapping
    (ChannelLinker/CategoryLinker, which stash the name at link time);
    `resolve_name(connector_id, entity_id)` instead resolves it live off the
    connector (EmoteLinker/UserLinker/RoleLinker, whose resolve hook can
    reflect a rename since linking)."""
    members = []
    for mapping in sorted(mappings, key=lambda m: (m.connector_id, getattr(m, id_attr))):  # type: ignore[attr-defined]
        info = connectors.get(mapping.connector_id)
        label = info.label if info else mapping.connector_id
        entity_id = getattr(mapping, id_attr)
        name = await resolve_name(mapping.connector_id, entity_id) if resolve_name else getattr(mapping, name_attr)
        members.append(LinkedMember(connector_id=mapping.connector_id, label=label, entity_id=entity_id, name=name))
    return members


async def format_linked_listing(
    mappings: "Iterable[object]",
    connectors: "dict[str, ConnectorInfo]",
    id_attr: str,
    name_attr: str | None = None,
    *,
    resolve_name: Callable[[str, str], Awaitable[str]] | None = None,
    marker_for: tuple[str, str] | None = None,
    marker_text: str = "",
) -> list[str]:
    """The line-per-member formatting shared by every `list_linked_*`
    method: one `"{label}: {name} ({id})"` line per mapping, sorted by
    `(connector_id, <id_attr>)` (via `collect_linked_members`).

    `marker_for`, a `(connector_id, id)` pair, appends `marker_text` to that
    one line - the "(this channel)"/"(this Category)" flag `/linked
    channels`/`/linked categories` put on the invoking entity. Unset for the
    live-resolved linkers, which have no such "this one" context. The id is
    dropped from a line whenever it's identical to the resolved name (e.g.
    IRC, whose user_id already IS the display name) - only reachable when
    `marker_for` is unset, since every `marker_for` caller's `id_attr` is a
    real native id, never equal to its own name."""
    members = await collect_linked_members(mappings, connectors, id_attr, name_attr, resolve_name=resolve_name)
    lines = []
    for m in members:
        if marker_for is not None:
            marker = marker_text if (m.connector_id, m.entity_id) == marker_for else ""
            lines.append(f"{m.label}: {m.name} ({m.entity_id}){marker}")
        else:
            lines.append(f"{m.label}: {m.name}" if m.name == m.entity_id else f"{m.label}: {m.name} ({m.entity_id})")
    return lines


async def _kick_group_member(
    mapped: "Iterable[object]",
    destination: str,
    *,
    id_attr: str,
    not_a_member_message: str,
    delete_mapping: Callable[[str, str], Awaitable[None]],
    dissolve_survivors: Callable[[list], Awaitable[None]] | None = None,
) -> "tuple[object, list]":
    """The shared `/unlink <x>` "kick one member out" arithmetic every
    linker's `unlink_<kind>` runs once it's decided `destination` names a
    single group member rather than the whole group: find its mapping in
    `mapped` (LinkError, `not_a_member_message`, if it isn't one), delete
    just that mapping, then - when `dissolve_survivors` is given - drop the
    rest of the group too if that would leave at most one member behind ("a
    group of one isn't a bridge"). Only ChannelLinker/EmoteLinker/RoleLinker
    dissolve on a lone survivor; CategoryLinker/UserLinker don't (pass
    `dissolve_survivors=None`) - issue #106. Returns `(the kicked mapping,
    the survivors)` so a caller that needs them for something else
    (ChannelLinker's on_channel_unlinked announcement) doesn't recompute."""
    target = next((m for m in mapped if m.connector_id == destination), None)  # type: ignore[attr-defined]
    if target is None:
        raise LinkError(not_a_member_message)
    await delete_mapping(destination, getattr(target, id_attr))
    survivors = [m for m in mapped if m.connector_id != destination]  # type: ignore[attr-defined]
    if dissolve_survivors is not None and len(survivors) <= 1:
        await dissolve_survivors(survivors)
    return target, survivors


async def _unlink_all_groups(
    *,
    local_connector: str,
    destination: str | None,
    connectors: "dict[str, ConnectorInfo]",
    kind: str,
    name_attr: str,
    group_word: str,
    groups: dict[str, str],
    load_group: Callable[[str], Awaitable[list]],
    dissolve_group: Callable[[str, list], Awaitable[int]],
    kick_member: Callable[[str, list, str], Awaitable[object]],
    kind_plural: str | None = None,
) -> str:
    """The shared `/unlink <kind> all <service|all>` loop (issues #160,
    #181). `groups` maps every group id `local_connector` has a member in to
    the local member's display name. For each group, kick `destination`'s
    member out (skipping groups without one), or dissolve the group when
    `destination` is `all`. The group is re-read via `load_group` first, in
    case a concurrent command changed it. An omitted `destination` is
    rejected instead of meaning "dissolve everything", and a group that
    fails is reported as its own line rather than aborting the rest.

    `dissolve_group(group_id, members)` returns how many mappings it removed;
    `kick_member(group_id, members, destination)` returns the kicked mapping, whose
    `name_attr` is shown. `kind_plural` defaults to `kind` + "s"."""
    plural = kind_plural or f"{kind}s"
    if destination is None:
        raise LinkError("'all' needs an explicit service - name a connector, or 'all' to dissolve every group.")
    dissolve = _is_all_token(destination)

    def label(connector_id: str) -> str:
        return connectors[connector_id].label if connector_id in connectors else connector_id

    local_label = label(local_connector)
    if not groups:
        raise NothingLinkedError(f"no {plural} on {local_label} are linked to anything.")

    lines: list[str] = []
    done = 0
    for group_id, local_name in groups.items():
        prefix = f"'{local_name}'"
        try:
            mapped = await load_group(group_id)
            if not dissolve and not any(m.connector_id == destination for m in mapped):
                continue
            if dissolve:
                count = await dissolve_group(group_id, mapped)
                lines.append(f"{prefix}: dissolved its {group_word} ({count} {kind}(s) removed)")
            else:
                target = await kick_member(group_id, mapped, destination)
                lines.append(f"{prefix}: unlinked {label(destination)} {kind} '{getattr(target, name_attr)}'")
            done += 1
        except Exception as exc:  # report per group, don't abort the rest
            logger.exception("bulk unlink failed for %s %s", group_word, group_id)
            lines.append(f"{prefix}: failed - {exc}")

    if not lines:
        raise NothingLinkedError(f"none of {local_label}'s {plural} are linked to {label(destination)}.")
    header = "Dissolved" if dissolve else f"Unlinked {label(destination)} from"
    return f"{header} {done} {group_word}(s) on {local_label}:\n" + "\n".join(lines)
