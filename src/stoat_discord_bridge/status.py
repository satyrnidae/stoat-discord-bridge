"""Tracks per-connector connection health for the bridge's sync targets, and
renders it for the `/status` (Discord, Stoat) and `STATUS` (IRC DM) commands.

A target's state is derived from two inputs, kept by whichever sender/
receiver observes them:
  - connected: whether that connector's sender currently has a live
    connection (set from the sender's on_ready/on_disconnect handlers)
  - recent relay outcomes: whether `BridgeCoordinator` has been able to
    successfully post into that connector's receiver lately

Shared across the asyncio event loop (Discord/Stoat senders) and the IRC
bot's own thread (see services/irc_service.py), so mutations and reads go
through a lock.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from enum import Enum

# how many of the most recent relay attempts into a target factor into its state
_WINDOW = 20
# recent-failure counts (within _WINDOW) at which a connected target degrades/fails
_DEGRADED_AT = 1
_FAILING_AT = 5


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

    def __init__(self, labels: dict[str, str]) -> None:
        # `labels` maps every configured connector id to its display label
        # (from config.yaml, defaulting to the id itself) - also defines the
        # full set of tracked connectors.
        self._lock = threading.Lock()
        self._labels = dict(labels)
        self._targets = {connector_id: _TargetHealth() for connector_id in labels}

    def mark_connected(self, connector_id: str) -> None:
        with self._lock:
            self._targets[connector_id].connected = True

    def mark_disconnected(self, connector_id: str) -> None:
        with self._lock:
            self._targets[connector_id].connected = False

    def record_success(self, connector_id: str) -> None:
        with self._lock:
            self._targets[connector_id].record(True)

    def record_error(self, connector_id: str) -> None:
        with self._lock:
            self._targets[connector_id].record(False)

    def snapshot(self) -> dict[str, HealthState]:
        with self._lock:
            return {connector_id: target.state for connector_id, target in self._targets.items()}

    def render(self) -> str:
        return "\n".join(
            f"{_ICONS[state]} {self._labels[connector_id]}: {state.value}"
            for connector_id, state in self.snapshot().items()
        )
