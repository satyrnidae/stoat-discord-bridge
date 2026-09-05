from __future__ import annotations

from tests.fakes.fake_discord import FakeChannel, FakeClient, FakeGuild, FakeThread, FakeUser
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


