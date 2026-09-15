"""`VoiceBridgeCoordinator` - N-way voice bridging (issue #113), Phase 1:
classification + presence, no actual voice connecting yet.

This bridge runs a single bridge-group model: a bridge group whose linked
channels include >= 2 voice channels on voice-capable connectors is
*voice-bridgeable*, and the coordinator holds at most one live *session* -
bound to one such group - at a time. A session opens once >= 2 of its
voice-capable connectors have a non-bot occupant, joins/parts individual
connectors as people come and go, and closes once fewer than 2 remain
populated. If more than one group qualifies, the first (by bridge_group id)
wins; the coordinator never switches groups while a session is live.

Two independent inputs feed the presence table:
  - **Push**: each voice-capable sender calls `on_voice_presence` from its own
    connector's voice-state events (Discord's `on_voice_state_update`,
    Stoat's `voice_channel_join`/`leave`/`move`) - this is what makes a join/
    part take effect immediately.
  - **Pull**: `refresh_groups` re-derives which bridge groups are
    voice-bridgeable (via `ConnectorInfo.channel_is_voice`, since links can
    change) and re-seeds each one's presence from `ConnectorInfo.voice_occupants`
    - this is what seeds state at startup and self-heals drift in a
    connector's own cached voice-state (Stoat's especially, issue #66) between
    pushes. Called once at `start()` and every `_REFRESH_INTERVAL` thereafter.

`_open_session`/`_reconcile_membership`/`_close_session` drive the real
`VoiceConnector.join`/`VoiceTransport.close` calls (issue #113 Phase 2) - the
state machine above them (eligibility, first-group-wins, edge-triggered
opening) is unchanged from Phase 1. `voice_connectors` is optional so a
deployment that hasn't wired any yet (or a test exercising only
classification/presence) still gets Phase 1's behavior: state is tracked,
nothing is actually joined.

Joining a group is **not** atomic - `_open_session` joins every populated
connector it can, one at a time, and only rolls the whole attempt back
(closing anything that did connect) if fewer than 2 end up joined. A
mid-session join/part failure (`_reconcile_membership`) never triggers a
rollback of the *session* - a session that's already live stays live even if
one connector's join briefly fails, exactly as if that connector just hadn't
populated yet; the next presence/refresh pass retries it.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import TYPE_CHECKING

from stoat_discord_bridge.services.voice import pipeline
from stoat_discord_bridge.services.voice.base import VoiceJoinError

if TYPE_CHECKING:
    from stoat_discord_bridge.admin_commands import ConnectorInfo
    from stoat_discord_bridge.services.voice.base import VoiceConnector, VoiceTransport
    from stoat_discord_bridge.storage.channel_mappings import ChannelMappingRepository

logger = logging.getLogger(__name__)


def _channel_id_sort_key(channel_id: str) -> tuple:
    """Sort key for picking "the lowest id" among several voice channels on
    one connector (issue #113 assumption #7 - an unsupported config, just
    pick one deterministically and move on). A bare `min()` on the id
    strings is lexicographic, not numeric - wrong for Discord's decimal
    snowflake ids once they differ in digit count (`"10"` sorts before
    `"9"`). Purely-digit ids sort numerically; anything else (Stoat's ULIDs,
    which are lexicographically sortable *by design* - their leading
    characters encode a timestamp - so plain string order is already
    correct there) falls back to string order."""
    return (0, int(channel_id)) if channel_id.isdigit() else (1, channel_id)


class VoiceBridgeCoordinator:
    _REFRESH_INTERVAL = 60.0

    def __init__(
        self,
        channel_mappings: "ChannelMappingRepository",
        connectors: "dict[str, ConnectorInfo]",
        *,
        voice_connectors: "dict[str, VoiceConnector] | None" = None,
        follow_on_empty: bool = False,
    ) -> None:
        self._channel_mappings = channel_mappings
        self._connectors = connectors
        # `voice_connectors if ... is not None else {}`, NOT `voice_connectors
        # or {}` - bridge.py constructs an *empty* dict up front and hands it
        # in here specifically so it can keep populating it in place as each
        # sender is built (`voice_connectors[dc.id] = DiscordVoiceConnector(...)`,
        # after this constructor has already returned). An empty dict is
        # falsy, so `or {}` would silently discard that dict and bind this
        # attribute to an unrelated, permanently-empty one instead - every
        # later mutation in bridge.py would then never be visible here,
        # leaving `_join_connector` treating every connector as unwired (the
        # Phase 1 "trivially joined, no real transport" fallback) forever,
        # with `_open_session` reporting success despite never actually
        # joining anything.
        self._voice_connectors = voice_connectors if voice_connectors is not None else {}
        self._follow_on_empty = follow_on_empty
        # bridge_group -> {connector_id: channel_id} - every currently
        # voice-bridgeable group and its voice-capable member channels.
        self._voice_groups: dict[str, dict[str, str]] = {}
        # bridge_group -> {connector_id: {non-bot occupant user ids}}
        self._presence: dict[str, dict[str, set[str]]] = {}
        self._active_group: str | None = None
        self._active_connectors: set[str] = set()
        # connector_id -> its live VoiceTransport, for the active session only.
        self._transports: dict[str, "VoiceTransport"] = {}
        # The active session's shared audio pipeline (issue #113 Phase 3) -
        # created on the first real join of a session, torn down as a whole
        # in _close_session. None whenever no session is live, or when every
        # joined connector so far is a Phase 1 fallback (no real transport).
        self._mixer_clock: "pipeline.MixerClock | None" = None
        self._refresh_task: "asyncio.Task | None" = None
        # bridge_group -> was it eligible (>= 2 populated connectors) as of
        # the last _reevaluate() - see _reevaluate's docstring for why the
        # idle "pick a group" path is edge- rather than level-triggered.
        self._eligible_snapshot: dict[str, bool] = {}

    @property
    def active_group(self) -> str | None:
        return self._active_group

    @property
    def active_connectors(self) -> frozenset[str]:
        return frozenset(self._active_connectors)

    def voice_bridgeable_groups(self) -> dict[str, dict[str, str]]:
        """A snapshot of every voice-bridgeable bridge group ->
        {connector_id: channel_id} - for `/linked channels`' "(voice)" tag
        and a `/voice status` command."""
        return {group: dict(members) for group, members in self._voice_groups.items()}

    def occupants(self, group: str) -> dict[str, frozenset[str]]:
        """A snapshot of `group`'s current per-connector occupant sets."""
        return {connector_id: frozenset(users) for connector_id, users in self._presence.get(group, {}).items()}

    async def start(self) -> None:
        """Seed state and start the periodic re-derive loop. Safe to call
        again later (e.g. a restart) - any previously running loop is
        cancelled first rather than leaked alongside a new one."""
        await self._cancel_refresh_task()
        await self.refresh_groups()
        self._refresh_task = asyncio.create_task(self._refresh_loop())

    async def close(self) -> None:
        await self._cancel_refresh_task()
        await self._close_session()

    async def _cancel_refresh_task(self) -> None:
        if self._refresh_task is None:
            return
        self._refresh_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._refresh_task
        self._refresh_task = None

    async def _refresh_loop(self) -> None:
        while True:
            await asyncio.sleep(self._REFRESH_INTERVAL)
            try:
                await self.refresh_groups()
            except Exception:
                logger.exception("[voice] periodic refresh failed")

    async def refresh_groups(self) -> None:
        """Re-derive which bridge groups are voice-bridgeable and re-seed
        their presence. Safe to call any time (link/unlink, reconnect, or the
        periodic loop) - it's a full re-derivation, not an incremental one."""
        mappings = await self._channel_mappings.get_all()
        by_group: dict[str, list] = {}
        for mapping in mappings:
            by_group.setdefault(mapping.bridge_group, []).append(mapping)

        voice_groups: dict[str, dict[str, str]] = {}
        for group, members in by_group.items():
            per_connector: dict[str, list[str]] = {}
            for member in members:
                info = self._connectors.get(member.connector_id)
                if info is None:
                    logger.debug("[voice] group %r: connector %s isn't wired at all", group, member.connector_id)
                    continue
                if info.channel_is_voice is None:
                    logger.debug(
                        "[voice] group %r: connector %s has no channel_is_voice hook "
                        "(voice.enabled or this connector's voice_bridging is off)",
                        group,
                        member.connector_id,
                    )
                    continue
                try:
                    is_voice = await info.channel_is_voice(member.channel_id)
                except Exception:
                    logger.exception(
                        "[voice] channel_is_voice failed for %s/%s", member.connector_id, member.channel_id
                    )
                    is_voice = None
                if not is_voice:
                    logger.debug(
                        "[voice] group %r: %s/%s isn't classified as a voice channel (channel_is_voice -> %r)",
                        group,
                        member.connector_id,
                        member.channel_id,
                        is_voice,
                    )
                    continue
                per_connector.setdefault(member.connector_id, []).append(member.channel_id)

            resolved: dict[str, str] = {}
            for connector_id, channel_ids in per_connector.items():
                lowest = min(channel_ids, key=_channel_id_sort_key)
                if len(channel_ids) > 1:
                    logger.warning(
                        "[voice] bridge group %r has %d voice channels on connector %s - "
                        "using the lowest id (%s), ignoring the rest",
                        group,
                        len(channel_ids),
                        connector_id,
                        lowest,
                    )
                resolved[connector_id] = lowest
            if len(resolved) >= 2:
                voice_groups[group] = resolved
            elif resolved:
                logger.debug(
                    "[voice] group %r: only %d voice-classified connector(s) (%s) - not voice-bridgeable",
                    group,
                    len(resolved),
                    sorted(resolved),
                )

        self._voice_groups = voice_groups
        # Drop presence bookkeeping for groups/connectors no longer voice-bridgeable.
        for group in list(self._presence):
            if group not in voice_groups:
                del self._presence[group]
                continue
            for connector_id in set(self._presence[group]) - set(voice_groups[group]):
                del self._presence[group][connector_id]

        for group, members in voice_groups.items():
            for connector_id, channel_id in members.items():
                info = self._connectors[connector_id]
                if info.voice_occupants is None:
                    continue
                try:
                    occupants = await info.voice_occupants(channel_id)
                except Exception:
                    logger.exception("[voice] voice_occupants failed for %s/%s", connector_id, channel_id)
                    occupants = None
                if occupants is not None:
                    self._presence.setdefault(group, {})[connector_id] = set(occupants)

        await self._reevaluate()

    async def on_voice_presence(
        self, connector_id: str, channel_id: str, user_id: str, *, present: bool, is_bot: bool
    ) -> None:
        """A user joined/left a voice channel on `connector_id`, pushed live
        by that connector's own sender. Ignored if `is_bot`, or if the
        channel isn't currently a voice member of any voice-bridgeable
        group (unlinked, or the group hasn't reached 2 voice channels)."""
        if is_bot:
            return
        group = self._group_for(connector_id, channel_id)
        if group is None:
            return
        occupants = self._presence.setdefault(group, {}).setdefault(connector_id, set())
        if present:
            occupants.add(user_id)
        else:
            occupants.discard(user_id)
        await self._reevaluate()

    def _group_for(self, connector_id: str, channel_id: str) -> str | None:
        for group, members in self._voice_groups.items():
            if members.get(connector_id) == channel_id:
                return group
        return None

    def _populated_connectors(self, group: str) -> set[str]:
        """Which of `group`'s voice-capable connectors currently have >= 1
        non-bot occupant."""
        return {connector_id for connector_id, users in self._presence.get(group, {}).items() if users}

    def _is_eligible(self, group: str) -> bool:
        return len(self._populated_connectors(group)) >= 2

    async def _reevaluate(self) -> None:
        """The state machine's single decision point, run after every
        presence change (push or pull).

        The "no active session -> open the first eligible group" rule is
        deliberately **edge-triggered**: a group only auto-opens on the
        reevaluate where it *newly* crosses into eligibility (below 2
        populated connectors, now at/above), never merely because it
        already happens to be eligible while the coordinator is idle. That's
        what stops a second group that was already sitting eligible during
        another group's live session from being opportunistically grabbed
        the instant that session ends - "the bot does not switch groups
        while a session is live" (and, by the same logic, doesn't queue-jump
        into a waiting one right after). `follow_on_empty` is the one
        explicit exception: it's a level-check ("is some other group already
        eligible right now"), run only at the moment a session just closed.
        """
        current_eligible = {group: self._is_eligible(group) for group in self._voice_groups}
        # Groups whose _open_session was attempted-and-rolled-back this pass
        # (fewer than 2 connectors actually joined) - excluded below from
        # what gets recorded as "seen eligible" so the *next* reevaluate
        # still treats them as newly-eligible and retries, instead of the
        # edge-trigger permanently locking them out after one failed
        # attempt (they'd otherwise need to drop below eligibility and rise
        # again before ever being tried a second time).
        failed_to_open: set[str] = set()

        if self._active_group is not None:
            if self._active_group not in self._voice_groups or not current_eligible.get(self._active_group, False):
                closed = self._active_group
                await self._close_session()
                if self._follow_on_empty:
                    # Same fallthrough as the idle branch below: a rolled-back
                    # open tries the next eligible group instead of leaving
                    # the coordinator idle despite a qualifying group.
                    for group in sorted(self._voice_groups):
                        if group == closed or not current_eligible.get(group, False):
                            continue
                        if await self._open_session(group):
                            break
                        failed_to_open.add(group)
            else:
                await self._reconcile_membership()
        else:
            for group in sorted(self._voice_groups):
                if current_eligible.get(group, False) and not self._eligible_snapshot.get(group, False):
                    if await self._open_session(group):
                        break
                    # Every connector that could join failed (or fewer than
                    # 2 came up) and the attempt was rolled back - fall
                    # through to the next eligible group instead of leaving
                    # the coordinator idle despite a qualifying group.
                    failed_to_open.add(group)

        self._eligible_snapshot = {
            group: eligible and group not in failed_to_open for group, eligible in current_eligible.items()
        }

    async def _open_session(self, group: str) -> bool:
        """Join every populated voice-capable connector in `group`. Returns
        whether a session actually came up - False (after rolling back
        anything that did connect) if fewer than 2 connectors joined, so the
        caller can fall through to the next eligible group.

        A connector with no `VoiceConnector` wired at all (`voice_connectors`
        wasn't passed, or doesn't cover this connector) is a Phase 1
        fallback: it's treated as trivially joined with no real transport,
        so a deployment (or test) that hasn't wired real voice yet keeps
        Phase 1's exact tracking-only behavior. A connector that *is* wired
        but currently unavailable (`voice_available` False) is excluded
        before any join is even attempted - a group that can't reach 2
        available connectors shouldn't cause pointless connect-then-
        immediately-disconnect churn on the ones that are."""
        wanted = self._populated_connectors(group)
        eligible = sorted(c for c in wanted if self._voice_available(c))
        if len(eligible) < 2:
            logger.warning(
                "[voice] bridge group %r only has %d voice-available connector(s) (wanted %s) - not opening",
                group,
                len(eligible),
                sorted(wanted),
            )
            return False

        joined: dict[str, "VoiceTransport | None"] = {}
        for connector_id in eligible:
            ok, transport = await self._join_connector(group, connector_id)
            if ok:
                joined[connector_id] = transport

        if len(joined) < 2:
            logger.warning(
                "[voice] bridge group %r only reached %d joined connector(s) (wanted %s) - rolling back",
                group,
                len(joined),
                sorted(wanted),
            )
            for transport in joined.values():
                if transport is not None:
                    await self._safe_close(transport)
            # A rolled-back attempt leaves no session - don't leave its
            # mixer clock's tick loop running with nothing to feed.
            if self._mixer_clock is not None:
                await self._mixer_clock.close()
                self._mixer_clock = None
            return False

        self._active_group = group
        self._active_connectors = set(joined)
        self._transports = {cid: t for cid, t in joined.items() if t is not None}
        logger.info("[voice] opened session for bridge group %r on connectors %s", group, sorted(joined))
        return True

    async def _reconcile_membership(self) -> None:
        group = self._active_group
        assert group is not None
        wanted = self._populated_connectors(group)
        for connector_id in sorted(wanted - self._active_connectors):
            ok, transport = await self._join_connector(group, connector_id)
            if ok:
                if transport is not None:
                    self._transports[connector_id] = transport
                self._active_connectors.add(connector_id)
        for connector_id in sorted(self._active_connectors - wanted):
            logger.info("[voice] connector %s emptied - parting session for group %r", connector_id, group)
            self._active_connectors.discard(connector_id)
            transport = self._transports.pop(connector_id, None)
            if transport is not None:
                await self._safe_close(transport)

    async def _close_session(self) -> None:
        if self._active_group is None:
            return
        logger.info(
            "[voice] closing session for bridge group %r (was on connectors %s)",
            self._active_group,
            sorted(self._active_connectors),
        )
        self._active_group = None
        self._active_connectors = set()
        transports, self._transports = self._transports, {}
        for transport in transports.values():
            await self._safe_close(transport)
        if self._mixer_clock is not None:
            await self._mixer_clock.close()
            self._mixer_clock = None

    async def connector_disconnected(self, connector_id: str) -> None:
        """`connector_id`'s sender went offline (gateway dropped). Clears its
        presence in every group so a live session doesn't keep counting
        stale occupants, and (best-effort, via `_safe_close`) closes its
        live transport if it was part of the active session - even a
        connection that's already dead alongside the dropped gateway should
        get a chance to release its own resources, and `_safe_close` can't
        let that raise into this cleanup path either way. Call
        `refresh_groups()` again once the connector reconnects to re-seed
        its presence."""
        changed = False
        for group in self._presence:
            if self._presence[group].pop(connector_id, None) is not None:
                changed = True
        if self._active_group is not None and connector_id in self._active_connectors:
            self._active_connectors.discard(connector_id)
            transport = self._transports.pop(connector_id, None)
            if transport is not None:
                await self._safe_close(transport)
            changed = True
        if changed:
            await self._reevaluate()

    def _voice_available(self, connector_id: str) -> bool:
        """Whether `connector_id` could join voice right now - True for a
        connector with no `VoiceConnector` wired at all (Phase 1 fallback,
        see `_open_session`'s docstring), otherwise that connector's own
        `voice_available`."""
        connector = self._voice_connectors.get(connector_id)
        return connector is None or connector.voice_available

    async def _join_connector(self, group: str, connector_id: str) -> "tuple[bool, VoiceTransport | None]":
        """Join `connector_id`'s channel for `group`. `(True, None)` if no
        `VoiceConnector` is wired for this connector (Phase 1 fallback -
        tracked as joined, nothing real to close later); `(True, transport)`
        on a real successful join; `(False, None)`, logged, if the connector
        is wired but unavailable, its channel can't be resolved, or its
        `join()` call itself failed."""
        connector = self._voice_connectors.get(connector_id)
        if connector is None:
            return True, None
        if not connector.voice_available:
            logger.warning(
                "[voice] connector %s isn't voice-available - skipping join for group %r", connector_id, group
            )
            return False, None
        channel_id = self._voice_groups.get(group, {}).get(connector_id)
        if channel_id is None:
            logger.warning(
                "[voice] connector %s has no voice channel recorded for group %r - can't join", connector_id, group
            )
            return False, None
        try:
            transport = await connector.join(channel_id)
        except VoiceJoinError:
            logger.exception("[voice] failed to join connector %s for group %r", connector_id, group)
            return False, None
        await self._wire_audio(connector_id, transport)
        return True, transport

    async def _wire_audio(self, connector_id: str, transport: "VoiceTransport") -> None:
        """Attach the session's shared audio pipeline to a freshly-joined
        transport (issue #113 Phase 3): the mixer clock is created lazily on
        a session's first real join and shared by every connector joined
        into it afterward (`_reconcile_membership`'s mid-session joins
        included) - `_close_session`/`_open_session`'s rollback are what
        tear it down as a whole."""
        if self._mixer_clock is None:
            self._mixer_clock = pipeline.MixerClock()
            await self._mixer_clock.start()
        holder = self._mixer_clock.add_connector(connector_id)
        transport.set_output(holder)
        await transport.start(self._on_speaker_frame)

    async def _on_speaker_frame(self, connector_id: str, user_id: str, frame: bytes) -> None:
        """The callback every joined transport's `start()` is wired to -
        just routes a received speaker frame into the session's shared
        `SpeakerRegistry`. `_mixer_clock` should never actually be `None`
        here (a transport can't be receiving without having gone through
        `_wire_audio` first), but the guard costs nothing and avoids a crash
        racing a session teardown against an in-flight frame."""
        if self._mixer_clock is not None:
            self._mixer_clock.registry.push_frame(connector_id, user_id, frame)

    async def _safe_close(self, transport: "VoiceTransport") -> None:
        """Close `transport`, then drop it from the mixer clock - in that
        order. Removing it first would let any speaker frame still in
        flight (e.g. mid-`_on_speaker_frame` when cancellation is issued)
        call `SpeakerRegistry.push_frame`, which get-or-creates a buffer for
        this connector - one `remove_connector` already ran and will never
        run again for it, leaking a stale registry entry for the rest of
        the session. `transport.close()` stops frame production first, so
        there's nothing left to race the removal."""
        try:
            await transport.close()
        except Exception:
            logger.exception("[voice] error closing voice transport for connector %s", transport.connector_id)
        if self._mixer_clock is not None:
            self._mixer_clock.remove_connector(transport.connector_id)
