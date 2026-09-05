from __future__ import annotations

from tests.fakes.fake_stoat import FakeCategory, FakeChannel
from tests.stoat_admin.conftest import FakeCategoryLinker, _make_sender


# ---------------------------------------------------------------- _handle_channel_create


async def test_handle_channel_create_syncs_a_new_channel_in_a_linked_category():
    category_linker = FakeCategoryLinker()
    sender = _make_sender(category_linker=category_linker, server_id="s1")
    category = FakeCategory(id="cat-1", title="Team")
    channel = FakeChannel(id="c2", name="general-2", server_id="s1", category=category)

    await sender._handle_channel_create(channel)

    assert category_linker.sync_new_channel_calls == [
        {
            "local_connector": "stoat",
            "local_category_id": "cat-1",
            "channel_id": "c2",
            "channel_name": "general-2",
        }
    ]


async def test_handle_channel_create_noop_without_a_configured_category_linker():
    sender = _make_sender(category_linker=None, server_id="s1")
    category = FakeCategory(id="cat-1", title="Team")
    channel = FakeChannel(id="c2", name="general-2", server_id="s1", category=category)

    await sender._handle_channel_create(channel)  # would raise if it tried to use a None category_linker


async def test_handle_channel_create_noop_for_a_channel_on_a_different_server():
    category_linker = FakeCategoryLinker()
    sender = _make_sender(category_linker=category_linker, server_id="s1")
    category = FakeCategory(id="cat-1", title="Team")
    channel = FakeChannel(id="c2", name="general-2", server_id="other-server", category=category)

    await sender._handle_channel_create(channel)

    assert category_linker.sync_new_channel_calls == []


async def test_handle_channel_create_noop_for_a_channel_with_no_category():
    category_linker = FakeCategoryLinker()
    sender = _make_sender(category_linker=category_linker, server_id="s1")
    channel = FakeChannel(id="c2", name="general-2", server_id="s1", category=None)

    await sender._handle_channel_create(channel)

    assert category_linker.sync_new_channel_calls == []
