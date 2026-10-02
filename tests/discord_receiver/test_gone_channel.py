"""A relay into a Discord channel that 404s raises RelayTargetGoneError, so
BridgeCoordinator can drop the stale link (issue #217). A Forbidden channel
still exists - the bot just lost access - so it's never reported as gone."""

from __future__ import annotations

import discord
import pytest

from stoat_discord_bridge.services.base import RelayTargetGoneError
from tests.discord_receiver.conftest import _edit, _make_receiver, _message
from tests.fakes.fake_discord import FakeClient, discord_forbidden


@pytest.fixture
def receiver():
    # channel 42 isn't in the fake client, so fetch_channel 404s
    return _make_receiver(FakeClient())


async def test_receive_into_a_deleted_channel(receiver):
    with pytest.raises(RelayTargetGoneError):
        await receiver.receive(_message(), target_channel_id="42")


async def test_edit_and_delete_into_a_deleted_channel(receiver):
    with pytest.raises(RelayTargetGoneError):
        await receiver.edit_message(target_channel_id="42", target_message_ids=["1"], edit=_edit())
    with pytest.raises(RelayTargetGoneError):
        await receiver.delete_message(target_channel_id="42", target_message_ids=["1"])


async def test_pin_and_reactions_into_a_deleted_channel(receiver):
    with pytest.raises(RelayTargetGoneError):
        await receiver.set_pinned(target_channel_id="42", target_message_id="1", pinned=True)
    with pytest.raises(RelayTargetGoneError):
        await receiver.add_reaction(target_channel_id="42", target_message_id="1", emoji="x")
    with pytest.raises(RelayTargetGoneError):
        await receiver.remove_reaction(target_channel_id="42", target_message_id="1", emoji="x")


async def test_a_forbidden_channel_is_not_gone():
    client = FakeClient()

    async def fetch_channel(_channel_id):
        raise discord_forbidden()

    client.fetch_channel = fetch_channel
    receiver = _make_receiver(client)

    with pytest.raises(discord.Forbidden):
        await receiver.receive(_message(), target_channel_id="42")
