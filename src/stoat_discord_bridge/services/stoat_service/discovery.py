"""Deployment-URL discovery for a Stoat connector.

`stoat.Client` defaults its websocket and CDN base URLs to the *public*
hosted instance's regardless of the `http_base` it's given, which is
silently wrong for a self-hosted deployment. Every stoat.py-compatible
server exposes a "NodeInfo"-style document at its REST root that reports the
real URLs; `_StoatClient.__init__` fetches it once at construction and feeds
the two `_discover_*` readers below.
"""

from __future__ import annotations

import json
import logging
import urllib.request

logger = logging.getLogger(__name__)


def _discover_node_config(http_base: str, *, connector_id: str = "stoat") -> dict | None:
    """Fetches the "NodeInfo"-style config document every stoat.py-compatible
    server exposes at its REST root - used to discover deployment-specific
    URLs that stoat.Client otherwise defaults to the *public* hosted
    instance's for, regardless of `http_base` (see `_discover_websocket_base`
    and `_discover_cdn_base`, both fed from this one fetch rather than each
    hitting the network separately). Best-effort: returns None on any
    failure - network hiccup, unexpected shape, whatever - so callers fall
    back to stoat.Client's own (public-instance) defaults rather than
    blocking startup on it. Unlike that silent fallback, though, the failure
    itself is logged (not swallowed) - a self-hosted deployment silently
    stuck on the public instance's URLs is exactly the failure mode this
    function exists to avoid, so a reverse proxy/WAF rejection, a self-signed
    cert, or a REST root that isn't actually the NodeInfo document all need
    to be visible, not just "avatars never load".

    Sends a real User-Agent (urllib's default, "Python-urllib/x.y", is a
    common bot-blocklist target for reverse proxies/CDNs fronting a
    self-hosted deployment - a 403 for that reason looks identical to a
    genuine network failure without this).
    """
    url = http_base.rstrip("/")
    request = urllib.request.Request(url, headers={"User-Agent": f"stoat-discord-bridge ({connector_id})"})
    try:
        with urllib.request.urlopen(request, timeout=10) as resp:
            body = resp.read()
    except Exception:
        logger.warning(
            "[stoat:%s] couldn't reach '%s' to discover its real websocket/CDN URLs - falling back to the "
            "public instance's, which is wrong for a self-hosted deployment",
            connector_id,
            url,
            exc_info=True,
        )
        return None
    try:
        return json.loads(body)
    except Exception:
        logger.warning(
            "[stoat:%s] '%s' didn't return the expected NodeInfo JSON - falling back to the public instance's "
            "websocket/CDN URLs, which is wrong for a self-hosted deployment; response started with %r",
            connector_id,
            url,
            body[:200],
            exc_info=True,
        )
        return None


def _discover_websocket_base(node_config: dict | None) -> str | None:
    """stoat.Client's `websocket_base` defaults to the public hosted
    instance's gateway (wss://events.stoat.chat/) regardless of `http_base`
    - correct for the public deployment (whose real gateway happens to live
    on that exact domain) but silently wrong for a self-hosted one, which
    then just hangs forever waiting on a response from a server that was
    never going to answer for that token, with no error to show for it.

    Every deployment's REST root reports its actual gateway URL in a `ws`
    field, so use that instead of assuming the public one.
    """
    if node_config is None:
        return None
    ws = node_config.get("ws")
    return ws if isinstance(ws, str) and ws else None


def _discover_cdn_base(node_config: dict | None) -> str | None:
    """stoat.Client's `cdn_base` - which every avatar/attachment/custom-emoji
    URL this bridge builds (via Asset.url()) goes through - defaults to the
    public hosted instance's CDN (`cdn.stoatusercontent.com`, hardcoded in
    stoat.py's CDNClient) regardless of `http_base`, same class of bug as
    `websocket_base` above. For a self-hosted deployment this means every
    asset URL silently points at the wrong server's CDN and never resolves
    - the images just don't exist there - with no error, since URL
    construction itself can't fail.

    Every deployment's REST root reports its actual CDN ("autumn", Revolt's
    - and by extension stoat.py's - name for this microservice) URL at
    features.autumn.url, so use that instead of assuming the public one.
    """
    if node_config is None:
        return None
    try:
        url = node_config["features"]["autumn"]["url"]
    except (KeyError, TypeError):
        return None
    return url if isinstance(url, str) and url else None
