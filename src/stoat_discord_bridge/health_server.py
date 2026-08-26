"""Minimal HTTP endpoint for Docker's HEALTHCHECK (see the Dockerfile) - or
any other external monitor - to poll.

Deliberately liveness-only: GET /healthz returns 200 as long as the aiohttp
server itself is answering, which is enough to prove the event loop isn't
deadlocked/blocked. It does NOT reflect per-connector connection state
(HealthTracker) - a transient IRC reconnect or Discord gateway hiccup
shouldn't flip the whole container to "unhealthy" and get it restarted,
which would also kill every other, still-fine connector along with it.
Per-connector state stays available via the existing /status (Discord slash)
/ STATUS (IRC DM) commands, plus GET /status here mirroring the same
HealthTracker.snapshot() as plain JSON for any external monitoring that
wants finer detail than a pass/fail.
"""

from __future__ import annotations

import os

from aiohttp import web

from stoat_discord_bridge.status import HealthTracker

_DEFAULT_PORT = 8080


async def _healthz(_request: web.Request) -> web.Response:
    return web.Response(text="ok")


def _make_status_handler(health: HealthTracker):
    async def _status(_request: web.Request) -> web.Response:
        snapshot = health.snapshot()
        return web.json_response({connector_id: state.value for connector_id, state in snapshot.items()})

    return _status


def _build_app(health: HealthTracker) -> web.Application:
    app = web.Application()
    app.router.add_get("/healthz", _healthz)
    app.router.add_get("/status", _make_status_handler(health))
    return app


async def start_health_server(health: HealthTracker) -> web.AppRunner:
    """Binds immediately and returns the running AppRunner - caller is
    responsible for `await runner.cleanup()` on shutdown."""
    port = int(os.environ.get("HEALTH_PORT", _DEFAULT_PORT))
    runner = web.AppRunner(_build_app(health))
    await runner.setup()
    site = web.TCPSite(runner, host="0.0.0.0", port=port)
    await site.start()
    return runner
