"""Sender-side name-colour resolution for issue #74.

Both `_member_colour` helpers are network-free (they read an
already-resolved top role off the message author), so these tests build the
author shapes by hand - no client, no fake gateway. The `color_forwarding`
gate on each sender's `_resolve_sender_color` is covered too.

Receiver-side application (the value landing on a Stoat masquerade, and the
`manage_roles`-rejection retry) lives in test_stoat_receiver.py.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from stoat_discord_bridge.services.discord_service.formatting import _member_colour as _discord_member_colour
from stoat_discord_bridge.services.stoat_service.formatting import _member_colour as _stoat_member_colour


# ---------------------------------------------------------------- Discord

@pytest.mark.parametrize(
    "value, expected",
    [
        (0x5865F2, "#5865f2"),
        (0xFF0000, "#ff0000"),
        (0x000001, "#000001"),
        (0, None),  # Colour.default() - "no colour"
    ],
)
def test_discord_member_colour_from_the_resolved_role_colour(value, expected):
    author = SimpleNamespace(colour=SimpleNamespace(value=value))
    assert _discord_member_colour(author) == expected


def test_discord_member_colour_none_when_author_has_no_colour_attr():
    assert _discord_member_colour(SimpleNamespace()) is None


# ---------------------------------------------------------------- Stoat

def _role(color, rank):
    return SimpleNamespace(color=color, rank=rank)


def test_stoat_member_colour_picks_the_lowest_rank_coloured_role():
    # rank is ascending-priority: rank 1 outranks rank 5.
    author = SimpleNamespace(roles=[_role("#aaa", 5), _role("#111", 1), _role(None, 0)])
    assert _stoat_member_colour(author) == "#111"


def test_stoat_member_colour_passes_a_gradient_string_straight_through():
    author = SimpleNamespace(roles=[_role("linear-gradient(to right, red, blue)", 2)])
    assert _stoat_member_colour(author) == "linear-gradient(to right, red, blue)"


def test_stoat_member_colour_none_when_no_role_has_a_colour():
    author = SimpleNamespace(roles=[_role(None, 1), _role("", 2)])
    assert _stoat_member_colour(author) is None


def test_stoat_member_colour_none_on_a_roles_read_that_raises():
    class _Boom:
        @property
        def roles(self):
            raise RuntimeError("cache miss")

    assert _stoat_member_colour(_Boom()) is None


# ---------------------------------------------------------------- gate

def test_discord_resolve_sender_color_gate():
    from stoat_discord_bridge.config import DiscordConnectorConfig
    from stoat_discord_bridge.services.discord_service import DiscordSenderService
    from stoat_discord_bridge.status import HealthTracker

    async def _noop(_m):
        pass

    author = SimpleNamespace(colour=SimpleNamespace(value=0x5865F2))
    for color_forwarding, expected in ((True, "#5865f2"), (False, None)):
        sender = DiscordSenderService(
            DiscordConnectorConfig(
                id="discord", label="Discord", guild_id=1, bot_token="t", color_forwarding=color_forwarding
            ),
            on_message=_noop,
            health=HealthTracker({"discord": "Discord"}),
        )
        assert sender._resolve_sender_color(author) == expected


def test_stoat_resolve_sender_color_gate():
    from stoat_discord_bridge.services.stoat_service import StoatSenderService

    message = SimpleNamespace(author=SimpleNamespace(roles=[_role("#111", 1)]))
    for color_forwarding, expected in ((True, "#111"), (False, None)):
        sender = object.__new__(StoatSenderService)
        sender._config = SimpleNamespace(color_forwarding=color_forwarding)
        assert sender._resolve_sender_color(message) == expected
