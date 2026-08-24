"""Tracks per-platform connection health for the bridge's sync targets, and
renders it for the `/status` (Discord, Stoat) and `STATUS` (IRC DM) commands.

A target's state is derived from two inputs, kept by whichever sender/
receiver observes them:
  - connected: whether that platform's sender currently has a live
    connection (set from the sender's on_ready/on_disconnect handlers)
  - recent relay outcomes: whether `BridgeCoordinator` has been able to
    successfully post into that platform's receiver lately

Shared across the asyncio event loop (Discord/Stoat senders) and the IRC
bot's own thread (see services/irc_service.py), so mutations and reads go
through a lock.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from enum import Enum

from stoat_discord_bridge.models import Platform

# how many of the most recent relay attempts into a target factor into its state
_WINDOW = 20
# recent-failure counts (within _WINDOW) at which a connected target degrades/fails
_DEGRADED_AT = 1
_FAILING_AT = 5

_LABELS = {
    Platform.DISCORD: "Discord",
    Platform.STOAT_PUBLIC: "Stoat (public)",
    Platform.STOAT_SELFHOSTED: "Stoat (self-hosted)",
    Platform.IRC: "IRC",
}


class HealthState(str, Enum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    FAILING = "failing"


_ICONS = {HealthState.HEALTHY: "\U0001f7e2", HealthState.DEGRADED: "\U0001f7e1", HealthState.FAILING: "\U0001f534"}


@dataclass
class _TargetHealth:
    connected: bool = False
    recent_results: list[bool] = field(default_factory=list)  # oldest first; True = relayed ok

    def record(self, success: bool) -> None:
        self.recent_results.append(success)
        del self.recent_results[:-_WINDOW]

    @property
    def state(self) -> HealthState:
        if not self.connected:
            return HealthState.FAILING
        failures = self.recent_results.count(False)
        if failures >= _FAILING_AT:
            return HealthState.FAILING
        if failures >= _DEGRADED_AT:
            return HealthState.DEGRADED
        return HealthState.HEALTHY


class HealthTracker:
    """Shared by every sender/receiver and `BridgeCoordinator`; the status
    commands read from this to answer `/status` / `STATUS`."""

    def __init__(self, platforms: list[Platform]) -> None:
        self._lock = threading.Lock()
        self._targets = {platform: _TargetHealth() for platform in platforms}

    def mark_connected(self, platform: Platform) -> None:
        with self._lock:
            self._targets[platform].connected = True

    def mark_disconnected(self, platform: Platform) -> None:
        with self._lock:
            self._targets[platform].connected = False

    def record_success(self, platform: Platform) -> None:
        with self._lock:
            self._targets[platform].record(True)

    def record_error(self, platform: Platform) -> None:
        with self._lock:
            self._targets[platform].record(False)

    def snapshot(self) -> dict[Platform, HealthState]:
        with self._lock:
            return {platform: target.state for platform, target in self._targets.items()}

    def render(self) -> str:
        return "\n".join(
            f"{_ICONS[state]} {_LABELS[platform]}: {state.value}" for platform, state in self.snapshot().items()
        )
