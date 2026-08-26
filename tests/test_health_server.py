"""Tests for health_server.py's aiohttp app - built via aiohttp's own test
utilities (TestServer/TestClient) rather than binding a real socket, so
these don't need a free port or an actual start_health_server() call."""

from __future__ import annotations

from aiohttp.test_utils import TestClient, TestServer

from stoat_discord_bridge.health_server import _build_app
from stoat_discord_bridge.status import HealthTracker


async def test_healthz_returns_200_regardless_of_connector_state():
    health = HealthTracker({"irc": "IRC"})  # never marked connected - would be "failing"
    app = _build_app(health)

    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/healthz")
        assert resp.status == 200


async def test_status_endpoint_mirrors_health_tracker_snapshot():
    health = HealthTracker({"irc": "IRC", "disc": "Discord"})
    health.mark_connected("irc")
    health.mark_disconnected("disc")
    app = _build_app(health)

    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/status")
        assert resp.status == 200
        body = await resp.json()

    assert body == {"irc": "healthy", "disc": "failing"}


async def test_status_endpoint_reflects_degraded_and_failing_from_relay_errors():
    health = HealthTracker({"a": "A"})
    health.mark_connected("a")
    health.record_error("a")
    app = _build_app(health)

    async with TestClient(TestServer(app)) as client:
        body = await (await client.get("/status")).json()

    assert body == {"a": "degraded"}
