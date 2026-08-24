"""_discover_websocket_base is the fix for a real bug (see its docstring):
stoat.Client silently defaults to the public hosted instance's gateway no
matter what http_base is, which just hangs forever against a self-hosted
deployment. Mocks urllib so this is testable without a live server.
"""

from __future__ import annotations

import json
from io import BytesIO
from unittest.mock import patch

from stoat_discord_bridge.services.stoat_service import _discover_websocket_base


def _fake_response(payload: dict):
    return BytesIO(json.dumps(payload).encode())


def test_returns_the_ws_field_when_present():
    with patch("urllib.request.urlopen") as mock_urlopen:
        mock_urlopen.return_value.__enter__.return_value = _fake_response({"ws": "wss://srv.example.net/ws"})
        assert _discover_websocket_base("https://srv.example.net/api") == "wss://srv.example.net/ws"


def test_strips_trailing_slash_before_fetching():
    with patch("urllib.request.urlopen") as mock_urlopen:
        mock_urlopen.return_value.__enter__.return_value = _fake_response({"ws": "wss://srv.example.net/ws"})
        _discover_websocket_base("https://srv.example.net/api/")
        called_url = mock_urlopen.call_args[0][0]
        assert called_url == "https://srv.example.net/api"


def test_returns_none_on_network_failure():
    with patch("urllib.request.urlopen", side_effect=OSError("connection refused")):
        assert _discover_websocket_base("https://srv.example.net/api") is None


def test_returns_none_when_ws_field_missing():
    with patch("urllib.request.urlopen") as mock_urlopen:
        mock_urlopen.return_value.__enter__.return_value = _fake_response({"other": "field"})
        assert _discover_websocket_base("https://srv.example.net/api") is None


def test_returns_none_when_ws_field_is_not_a_string():
    with patch("urllib.request.urlopen") as mock_urlopen:
        mock_urlopen.return_value.__enter__.return_value = _fake_response({"ws": None})
        assert _discover_websocket_base("https://srv.example.net/api") is None


def test_returns_none_on_malformed_json():
    with patch("urllib.request.urlopen") as mock_urlopen:
        mock_urlopen.return_value.__enter__.return_value = BytesIO(b"not json")
        assert _discover_websocket_base("https://srv.example.net/api") is None
