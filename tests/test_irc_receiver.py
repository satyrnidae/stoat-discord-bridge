"""Tests for IrcReceiverService against the fake_irc scaffolding
(tests/fakes/fake_irc.py) - receive()'s line-splitting/chunking and
partial-failure behavior. Previously untested (see coverage note in
test_irc_service.py, which covers IrcSenderService but never
IrcReceiverService).
"""

from __future__ import annotations

import pytest

from stoat_discord_bridge.models import StandardMessage
from stoat_discord_bridge.services.base import PartialRelayError
from stoat_discord_bridge.services.irc_service import IrcReceiverService
from stoat_discord_bridge.storage.user_mappings import UserMapping, UserMappingRepository
from tests.fakes.fake_irc import FakeIrcConnection


class _FakeSender:
    """IrcReceiverService only ever reads .connection - stand in without
    building a real IrcSenderService (whose _IrcClient needs a config)."""

    def __init__(self, connection: FakeIrcConnection, connector_id: str = "irc") -> None:
        self.connector_id = connector_id
        self.connection = connection


def _message(**overrides) -> StandardMessage:
    defaults = dict(
        origin_connector_id="discord",
        origin_channel_id="d-100",
        channel_name="general",
        sender_name="Alice",
        sender_avatar_url=None,
        sender_user_id="discord-alice",
        content_markdown="hello",
        message_id="m1",
    )
    defaults.update(overrides)
    return StandardMessage(**defaults)


def _make_receiver(connection: FakeIrcConnection, user_mappings: UserMappingRepository | None = None) -> IrcReceiverService:
    return IrcReceiverService(_FakeSender(connection), user_mappings=user_mappings)


async def test_receive_prefixes_each_line_with_the_senders_name():
    connection = FakeIrcConnection()
    receiver = _make_receiver(connection)

    ids = await receiver.receive(_message(content_markdown="line one\nline two"), target_channel_id="#general")

    assert connection.privmsg_calls == [
        ("#general", "<Alice> line one"),
        ("#general", "<Alice> line two"),
    ]
    assert len(ids) == 2
    assert len(set(ids)) == 2  # synthesized ids don't collide


async def test_receive_splits_a_line_longer_than_the_per_message_limit():
    connection = FakeIrcConnection()
    receiver = _make_receiver(connection)

    long_line = "x" * 500
    await receiver.receive(_message(content_markdown=long_line), target_channel_id="#general")

    assert len(connection.privmsg_calls) > 1
    assert all(len(text.encode()) <= 512 for _target, text in connection.privmsg_calls)


async def test_receive_sends_a_single_bare_line_when_content_is_empty():
    connection = FakeIrcConnection()
    receiver = _make_receiver(connection)

    ids = await receiver.receive(_message(content_markdown=""), target_channel_id="#general")

    assert connection.privmsg_calls == [("#general", "<Alice> ")]
    assert len(ids) == 1


async def test_receive_prefixes_with_the_linked_local_nick_when_linked(fake_db):
    user_mappings = UserMappingRepository(fake_db)
    await user_mappings.upsert(
        UserMapping(link_group="g1", connector_id="discord", user_id="discord-alice", display_name="discord-alice")
    )
    await user_mappings.upsert(UserMapping(link_group="g1", connector_id="irc", user_id="AliceIrc", display_name="AliceIrc"))
    connection = FakeIrcConnection()
    receiver = _make_receiver(connection, user_mappings)

    await receiver.receive(_message(), target_channel_id="#general")

    assert connection.privmsg_calls == [("#general", "<AliceIrc> hello")]


async def test_receive_uses_the_remote_name_when_the_sender_isnt_linked(fake_db):
    user_mappings = UserMappingRepository(fake_db)
    connection = FakeIrcConnection()
    receiver = _make_receiver(connection, user_mappings)

    await receiver.receive(_message(), target_channel_id="#general")

    assert connection.privmsg_calls == [("#general", "<Alice> hello")]


async def test_receive_raises_partial_relay_error_and_keeps_ids_already_sent():
    connection = FakeIrcConnection()
    receiver = _make_receiver(connection)

    call_count = 0
    real_privmsg = connection.privmsg

    def flaky_privmsg(target, text):
        nonlocal call_count
        call_count += 1
        if call_count == 2:
            raise RuntimeError("disconnected")
        real_privmsg(target, text)

    connection.privmsg = flaky_privmsg

    with pytest.raises(PartialRelayError) as exc_info:
        await receiver.receive(_message(content_markdown="line one\nline two"), target_channel_id="#general")

    assert len(exc_info.value.partial_ids) == 1
