"""A Stoat send or message fetch failing with stoat.NotFound is checked
against a fresh fetch_channel (only on that error path): a 404 there too
means the channel was deleted, raised as RelayTargetGoneError so the stale
link is dropped (issue #217). If the channel still resolves, the failure was
message-level and is handled as before. Stoat's live 404 shape for a deleted
channel is unverified against a real server, so the exceptions are faked."""

from __future__ import annotations

import pytest

from stoat_discord_bridge.models import StandardEdit
from stoat_discord_bridge.services.base import PartialRelayError, RelayTargetGoneError
from tests.fakes.fake_stoat import FakeClient, FakePartialMessageable, stoat_not_found
from tests.stoat_receiver.conftest import _make_receiver, _message


class _BrokenChannel(FakePartialMessageable):
    """Every send and message fetch 404s."""

    async def send(self, content, **kwargs):
        raise stoat_not_found()

    async def fetch_message(self, message_id):
        raise stoat_not_found()


def _setup(*, channel_exists: bool):
    client = FakeClient()
    channel = client.add_channel(_BrokenChannel("c1"))
    if not channel_exists:

        async def fetch_channel(_channel_id):
            raise stoat_not_found()

        client.fetch_channel = fetch_channel
    return _make_receiver(client), channel


def _edit() -> StandardEdit:
    return StandardEdit(
        origin_connector_id="discord", origin_channel_id="d-100", origin_message_id="m1", new_content_markdown="x"
    )


async def test_receive_into_a_deleted_channel():
    receiver, _ = _setup(channel_exists=False)

    with pytest.raises(RelayTargetGoneError):
        await receiver.receive(_message(), target_channel_id="c1")


async def test_receive_failure_in_a_live_channel_stays_a_partial_relay():
    receiver, _ = _setup(channel_exists=True)

    with pytest.raises(PartialRelayError):
        await receiver.receive(_message(), target_channel_id="c1")


async def test_edit_and_delete_into_a_deleted_channel():
    receiver, _ = _setup(channel_exists=False)

    with pytest.raises(RelayTargetGoneError):
        await receiver.edit_message(target_channel_id="c1", target_message_ids=["1", "2"], edit=_edit())
    with pytest.raises(RelayTargetGoneError):
        await receiver.delete_message(target_channel_id="c1", target_message_ids=["1", "2"])


async def test_edit_and_delete_of_missing_messages_in_a_live_channel_are_skipped():
    receiver, _ = _setup(channel_exists=True)

    await receiver.edit_message(target_channel_id="c1", target_message_ids=["1", "2"], edit=_edit())
    await receiver.delete_message(target_channel_id="c1", target_message_ids=["1", "2"])


async def test_pin_and_reactions_into_a_deleted_channel():
    receiver, _ = _setup(channel_exists=False)

    with pytest.raises(RelayTargetGoneError):
        await receiver.set_pinned(target_channel_id="c1", target_message_id="1", pinned=True)
    with pytest.raises(RelayTargetGoneError):
        await receiver.add_reaction(target_channel_id="c1", target_message_id="1", emoji="x")
    with pytest.raises(RelayTargetGoneError):
        await receiver.remove_reaction(target_channel_id="c1", target_message_id="1", emoji="x")


async def test_pin_and_reactions_on_a_missing_message_in_a_live_channel_are_skipped():
    receiver, _ = _setup(channel_exists=True)

    await receiver.set_pinned(target_channel_id="c1", target_message_id="1", pinned=True)
    await receiver.add_reaction(target_channel_id="c1", target_message_id="1", emoji="x")
    await receiver.remove_reaction(target_channel_id="c1", target_message_id="1", emoji="x")
