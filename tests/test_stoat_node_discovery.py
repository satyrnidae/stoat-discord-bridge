"""Tests for _discover_node_config/_discover_websocket_base/_discover_cdn_base
- the fix for two real bugs: stoat.Client defaults both its websocket
gateway *and* its CDN base to the public hosted instance's, regardless of
`http_base` - correct for the public deployment, silently wrong for a
self-hosted one (a hung connection for the websocket gateway; avatar/
attachment/emoji URLs that just never resolve, with no error, for the CDN
base since building an image URL for the wrong server can't itself fail).

Mocks urllib so the network-touching half (_discover_node_config) is
testable without a live server; the two field-extraction functions are pure
and operate on an already-parsed dict, so they need no mocking at all.
"""

from __future__ import annotations

import json
from io import BytesIO
from unittest.mock import patch

from stoat_discord_bridge.services.stoat_service import (
    _discover_cdn_base,
    _discover_node_config,
    _discover_websocket_base,
)


def _fake_response(payload: dict):
    return BytesIO(json.dumps(payload).encode())


# ---------------------------------------------------------------- _discover_node_config


def test_node_config_fetches_and_parses_the_root_document():
    with patch("urllib.request.urlopen") as mock_urlopen:
        mock_urlopen.return_value.__enter__.return_value = _fake_response({"ws": "wss://srv.example.net/ws"})
        assert _discover_node_config("https://srv.example.net/api") == {"ws": "wss://srv.example.net/ws"}


def test_node_config_strips_trailing_slash_before_fetching():
    with patch("urllib.request.urlopen") as mock_urlopen:
        mock_urlopen.return_value.__enter__.return_value = _fake_response({"ws": "wss://srv.example.net/ws"})
        _discover_node_config("https://srv.example.net/api/")
        called_request = mock_urlopen.call_args[0][0]
        assert called_request.full_url == "https://srv.example.net/api"


def test_node_config_sends_a_real_user_agent():
    # urllib's default User-Agent ("Python-urllib/x.y") is a common
    # bot-blocklist target for reverse proxies/CDNs fronting a self-hosted
    # deployment - a request with no override looks exactly like one of
    # those to the server, and gets rejected the same way.
    with patch("urllib.request.urlopen") as mock_urlopen:
        mock_urlopen.return_value.__enter__.return_value = _fake_response({"ws": "wss://srv.example.net/ws"})
        _discover_node_config("https://srv.example.net/api", connector_id="stoat_selfhosted")
        called_request = mock_urlopen.call_args[0][0]
        assert "python-urllib" not in called_request.get_header("User-agent").lower()
        assert "stoat_selfhosted" in called_request.get_header("User-agent")


def test_node_config_returns_none_on_network_failure():
    with patch("urllib.request.urlopen", side_effect=OSError("connection refused")):
        assert _discover_node_config("https://srv.example.net/api") is None


def test_node_config_logs_the_failure_reason_on_network_failure(caplog):
    with patch("urllib.request.urlopen", side_effect=OSError("connection refused")):
        with caplog.at_level("WARNING"):
            _discover_node_config("https://srv.example.net/api", connector_id="stoat_selfhosted")
    assert "stoat_selfhosted" in caplog.text
    assert "connection refused" in caplog.text


def test_node_config_returns_none_on_malformed_json():
    with patch("urllib.request.urlopen") as mock_urlopen:
        mock_urlopen.return_value.__enter__.return_value = BytesIO(b"not json")
        assert _discover_node_config("https://srv.example.net/api") is None


def test_node_config_logs_the_response_body_on_malformed_json(caplog):
    with patch("urllib.request.urlopen") as mock_urlopen:
        mock_urlopen.return_value.__enter__.return_value = BytesIO(b"<html>not json</html>")
        with caplog.at_level("WARNING"):
            _discover_node_config("https://srv.example.net/api", connector_id="stoat_selfhosted")
    assert "stoat_selfhosted" in caplog.text
    assert "<html>not json</html>" in caplog.text


# ---------------------------------------------------------------- _discover_websocket_base


def test_websocket_base_returns_the_ws_field_when_present():
    assert _discover_websocket_base({"ws": "wss://srv.example.net/ws"}) == "wss://srv.example.net/ws"


def test_websocket_base_returns_none_when_node_config_is_none():
    assert _discover_websocket_base(None) is None


def test_websocket_base_returns_none_when_ws_field_missing():
    assert _discover_websocket_base({"other": "field"}) is None


def test_websocket_base_returns_none_when_ws_field_is_not_a_string():
    assert _discover_websocket_base({"ws": None}) is None


# ---------------------------------------------------------------- _discover_cdn_base


def test_cdn_base_returns_the_autumn_url_when_present():
    node_config = {"features": {"autumn": {"enabled": True, "url": "https://cdn.srv.example.net"}}}
    assert _discover_cdn_base(node_config) == "https://cdn.srv.example.net"


def test_cdn_base_returns_none_when_node_config_is_none():
    assert _discover_cdn_base(None) is None


def test_cdn_base_returns_none_when_features_is_missing():
    assert _discover_cdn_base({"ws": "wss://srv.example.net/ws"}) is None


def test_cdn_base_returns_none_when_autumn_is_missing():
    assert _discover_cdn_base({"features": {}}) is None


def test_cdn_base_returns_none_when_url_field_is_not_a_string():
    assert _discover_cdn_base({"features": {"autumn": {"url": None}}}) is None


def test_cdn_base_returns_none_when_features_is_not_a_dict():
    assert _discover_cdn_base({"features": "unexpected"}) is None
