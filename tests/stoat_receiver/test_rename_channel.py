from __future__ import annotations

from tests.stoat_receiver.conftest import _make_receiver
from tests.fakes.fake_stoat import FakeChannel, FakeClient


# ---------------------------------------------------------------- rename_channel


async def test_rename_channel_renames_the_channel():
    client = FakeClient()
    channel = client.add_channel(FakeChannel(id="42", name="old-name"))
    receiver = _make_receiver(client)

    await receiver.rename_channel(target_channel_id="42", new_name="new-name")

    assert channel.name == "new-name"
    assert channel.edits == [{"name": "new-name"}]


async def test_rename_channel_is_a_noop_when_already_named_that():
    client = FakeClient()
    channel = client.add_channel(FakeChannel(id="42", name="same-name"))
    receiver = _make_receiver(client)

    await receiver.rename_channel(target_channel_id="42", new_name="same-name")

    assert channel.edits == []


async def test_rename_channel_clips_the_name_to_32_chars():
    client = FakeClient()
    channel = client.add_channel(FakeChannel(id="42", name="old-name"))
    receiver = _make_receiver(client)

    await receiver.rename_channel(target_channel_id="42", new_name="x" * 50)

    assert channel.name == "x" * 32


async def test_rename_channel_swallows_an_edit_failure():
    client = FakeClient()
    channel = client.add_channel(FakeChannel(id="42", name="old-name", raises=RuntimeError("nope")))
    receiver = _make_receiver(client)

    await receiver.rename_channel(target_channel_id="42", new_name="new-name")  # must not raise

    assert channel.name == "old-name"


async def test_rename_channel_is_a_noop_for_an_uncached_channel():
    client = FakeClient()  # nothing registered - get_channel(partial=True) yields a PartialMessageable
    receiver = _make_receiver(client)

    await receiver.rename_channel(target_channel_id="999", new_name="new-name")  # must not raise
