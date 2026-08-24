"""_avatar_url is the fix for a real bug: stoat.py's User/Member expose an
avatar as an Asset object with a .url() *method*, not a plain avatar_url
string attribute - the old code's getattr(author, "avatar_url", None)
always returned None, so every Stoat->Discord relay used the webhook's
default avatar regardless of the sender's real one.
"""

from __future__ import annotations

from types import SimpleNamespace

from stoat_discord_bridge.services.stoat_service import _avatar_url


def _asset(url: str):
    return SimpleNamespace(url=lambda: url)


def test_prefers_server_avatar_over_account_avatar():
    author = SimpleNamespace(
        server_avatar=_asset("https://cdn.example/server-avatar.png"),
        avatar=_asset("https://cdn.example/account-avatar.png"),
        default_avatar_url="https://cdn.example/default.png",
    )
    assert _avatar_url(author) == "https://cdn.example/server-avatar.png"


def test_falls_back_to_account_avatar_when_no_server_avatar():
    author = SimpleNamespace(
        server_avatar=None,
        avatar=_asset("https://cdn.example/account-avatar.png"),
        default_avatar_url="https://cdn.example/default.png",
    )
    assert _avatar_url(author) == "https://cdn.example/account-avatar.png"


def test_falls_back_to_default_avatar_when_neither_set():
    author = SimpleNamespace(server_avatar=None, avatar=None, default_avatar_url="https://cdn.example/default.png")
    assert _avatar_url(author) == "https://cdn.example/default.png"


def test_handles_a_plain_user_with_no_server_avatar_attribute_at_all():
    # a plain User (not a Member) has no server_avatar attribute at all,
    # not just None - getattr's default must cover this case too
    author = SimpleNamespace(avatar=_asset("https://cdn.example/account-avatar.png"))
    assert _avatar_url(author) == "https://cdn.example/account-avatar.png"


def test_returns_none_when_nothing_is_available():
    author = SimpleNamespace()
    assert _avatar_url(author) is None
