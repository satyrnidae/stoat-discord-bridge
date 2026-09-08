from __future__ import annotations

from types import SimpleNamespace

from tests.fakes.fake_discord import (
    FakeChannel,
    FakeClient,
    FakeForumChannel,
    FakeGuild,
    FakeThread,
    FakeUser,
)
from tests.discord_sender_dispatch.conftest import _Recorder, _make_sender


# ---------------------------------------------------------------- get_channel_name


async def test_get_channel_name_from_cache():
    client = FakeClient()
    client.add_channel(FakeChannel(id=42, name="general"))
    sender = _make_sender(_Recorder(), client)

    assert await sender.get_channel_name("42") == "general"


async def test_get_channel_name_returns_none_when_not_found():
    client = FakeClient()
    sender = _make_sender(_Recorder(), client)

    assert await sender.get_channel_name("999") is None


async def test_get_channel_name_returns_none_on_a_non_numeric_id():
    client = FakeClient()
    sender = _make_sender(_Recorder(), client)

    assert await sender.get_channel_name("not-a-number") is None


# ---------------------------------------------------------------- get_user_name


async def test_get_user_name_from_cache():
    client = FakeClient()
    client.add_user(FakeUser(id=216591124222050304, display_name="ShrinerH"))
    sender = _make_sender(_Recorder(), client)

    assert await sender.get_user_name("216591124222050304") == "ShrinerH"


async def test_get_user_name_returns_none_when_not_found():
    client = FakeClient()
    sender = _make_sender(_Recorder(), client)

    assert await sender.get_user_name("999") is None


async def test_get_user_name_returns_none_on_a_non_numeric_id():
    client = FakeClient()
    sender = _make_sender(_Recorder(), client)

    assert await sender.get_user_name("not-a-number") is None


# ---------------------------------------------------------------- get_thread_parent


async def test_get_thread_parent_returns_the_parent_id_and_name_for_a_thread():
    client = FakeClient()
    parent = FakeChannel(id=42, name="bot-config")
    client.add_channel(FakeThread(id=777, parent=parent, name="cool thread", guild=FakeGuild(id=123)))
    sender = _make_sender(_Recorder(), client)

    assert await sender.get_thread_parent("777") == ("42", "bot-config")


async def test_get_thread_parent_returns_none_for_a_plain_channel():
    client = FakeClient()
    client.add_channel(FakeChannel(id=42, name="general"))
    sender = _make_sender(_Recorder(), client)

    assert await sender.get_thread_parent("42") is None


async def test_get_thread_parent_returns_none_for_an_unresolvable_id():
    sender = _make_sender(_Recorder(), FakeClient())

    assert await sender.get_thread_parent("999") is None
    assert await sender.get_thread_parent("not-a-number") is None


# ---------------------------------------------------------------- is_forum_channel (issue #100)


async def test_is_forum_channel_true_for_a_forum_channel():
    client = FakeClient()
    client.add_channel(FakeForumChannel(id=42, name="ttrpg-forum"))
    sender = _make_sender(_Recorder(), client)

    assert await sender.is_forum_channel("42") is True


async def test_is_forum_channel_false_for_a_plain_channel_and_a_forum_post():
    client = FakeClient()
    forum = FakeForumChannel(id=42, name="ttrpg-forum")
    client.add_channel(FakeChannel(id=10, name="general"))
    client.add_channel(FakeThread(id=777, parent=forum, name="a post", guild=FakeGuild(id=123)))
    sender = _make_sender(_Recorder(), client)

    assert await sender.is_forum_channel("10") is False
    assert await sender.is_forum_channel("777") is False


async def test_is_forum_channel_none_for_an_unresolvable_id():
    sender = _make_sender(_Recorder(), FakeClient())

    assert await sender.is_forum_channel("999") is None
    assert await sender.is_forum_channel("not-a-number") is None


# ---------------------------------------------------------------- channels_in_category on a forum (issue #100)


async def test_channels_in_category_returns_a_forums_active_threads(monkeypatch):
    sender = _make_sender(_Recorder(), FakeClient())
    forum = FakeForumChannel(
        id=42,
        name="ttrpg-forum",
        threads=[SimpleNamespace(id=1, name="First Post"), SimpleNamespace(id=2, name="Second Post")],
    )
    monkeypatch.setattr(sender, "_guild_or_none", lambda: SimpleNamespace(get_channel=lambda cid: forum if cid == 42 else None))

    assert await sender.channels_in_category("42") == [("1", "First Post"), ("2", "Second Post")]


async def test_channels_in_category_returns_empty_for_a_non_category_non_forum(monkeypatch):
    sender = _make_sender(_Recorder(), FakeClient())
    monkeypatch.setattr(
        sender, "_guild_or_none", lambda: SimpleNamespace(get_channel=lambda cid: SimpleNamespace(id=cid))
    )

    assert await sender.channels_in_category("42") == []


