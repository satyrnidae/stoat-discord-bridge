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

`_open_session`/`_reconcile_membership`/`_close_session` only track *intended*
membership and log it in this phase - nothing actually connects to voice yet.
A later phase replaces their bodies to drive real `VoiceConnector.join`/
`close` calls without changing the state machine above them.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from stoat_discord_bridge.admin_commands import ConnectorInfo
    from stoat_discord_bridge.storage.channel_mappings import ChannelMappingRepository

logger = logging.getLogger(__name__)


class VoiceBridgeCoordinator:
    _REFRESH_INTERVAL = 60.0

    def __init__(
        self,
        channel_mappings: "ChannelMappingRepository",
        connectors: "dict[str, ConnectorInfo]",
        *,
        follow_on_empty: bool = False,
    ) -> None:
        self._channel_mappings = channel_mappings
        self._connectors = connectors
        self._follow_on_empty = follow_on_empty
        # bridge_group -> {connector_id: channel_id} - every currently
        # voice-bridgeable group and its voice-capable member channels.
        self._voice_groups: dict[str, dict[str, str]] = {}
        # bridge_group -> {connector_id: {non-bot occupant user ids}}
        self._presence: dict[str, dict[str, set[str]]] = {}
        self._active_group: str | None = None
        self._active_connectors: set[str] = set()
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
                if info is None or info.channel_is_voice is None:
                    continue
                try:
                    is_voice = await info.channel_is_voice(member.channel_id)
                except Exception:
                    logger.exception(
                        "[voice] channel_is_voice failed for %s/%s", member.connector_id, member.channel_id
                    )
                    is_voice = None
                if not is_voice:
                    continue
                per_connector.setdefault(member.connector_id, []).append(member.channel_id)

            resolved: dict[str, str] = {}
            for connector_id, channel_ids in per_connector.items():
                if len(channel_ids) > 1:
                    logger.warning(
                        "[voice] bridge group %r has %d voice channels on connector %s - "
                        "using the lowest id (%s), ignoring the rest",
                        group,
                        len(channel_ids),
                        connector_id,
                        min(channel_ids),
                    )
                resolved[connector_id] = min(channel_ids)
            if len(resolved) >= 2:
                voice_groups[group] = resolved

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

        if self._active_group is not None:
            if self._active_group not in self._voice_groups or not current_eligible.get(self._active_group, False):
                closed = self._active_group
                await self._close_session()
                if self._follow_on_empty:
                    for group in sorted(self._voice_groups):
                        if group != closed and current_eligible.get(group, False):
                            await self._open_session(group)
                            break
            else:
                await self._reconcile_membership()
        else:
            for group in sorted(self._voice_groups):
                if current_eligible.get(group, False) and not self._eligible_snapshot.get(group, False):
                    await self._open_session(group)
                    break

        self._eligible_snapshot = current_eligible

    async def _open_session(self, group: str) -> None:
        wanted = self._populated_connectors(group)
        self._active_group = group
        self._active_connectors = set(wanted)
        logger.info("[voice] opening session for bridge group %r on connectors %s", group, sorted(wanted))

    async def _reconcile_membership(self) -> None:
        group = self._active_group
        assert group is not None
        wanted = self._populated_connectors(group)
        for connector_id in sorted(wanted - self._active_connectors):
            logger.info("[voice] connector %s populated - joining session for group %r", connector_id, group)
        for connector_id in sorted(self._active_connectors - wanted):
            logger.info("[voice] connector %s emptied - parting session for group %r", connector_id, group)
        self._active_connectors = wanted

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
