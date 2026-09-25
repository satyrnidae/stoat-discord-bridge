"""Tests for BridgeCoordinator.backfill_history - the /mirror channel with
history (issue #122) orchestration step: fetch a source channel's history
via a fetch_history callable and relay it, in order, onto a single
destination only (never fanned out via handle_incoming).
"""

from __future__ import annotations

import stoat_discord_bridge.bridge as bridge_module
from stoat_discord_bridge.services.base import PartialRelayError, UnsupportedRelayTargetError
from tests.bridge.conftest import FakeReceiver, _message


def _fetch_history(messages):
    async def fetch(channel_id: str, limit: int | None):
        return list(messages)

    return fetch


async def test_backfill_history_relays_every_message_in_order(coordinator_parts, monkeypatch):
    monkeypatch.setattr(bridge_module, "_HISTORY_BACKFILL_PACING", 0)
    coordinator, *_ = coordinator_parts
    stoat_receiver = FakeReceiver("stoat")
    coordinator.register_receiver(stoat_receiver)
    messages = [_message(message_id="m1", content_markdown="first"), _message(message_id="m2", content_markdown="second")]

    summary = await coordinator.backfill_history(
        fetch_history=_fetch_history(messages),
        source_channel_id="100",
        destination_connector="stoat",
        destination_channel_id="200",
        limit=None,
    )

    assert [r[0].content_markdown for r in stoat_receiver.received] == ["first", "second"]
    assert all(r[1] == "200" for r in stoat_receiver.received)
    assert "relayed 2 message(s)" in summary


async def test_backfill_history_never_fans_out_to_other_bridge_members(coordinator_parts, monkeypatch):
    # Even if the source channel is already bridged to a third connector via
    # channel_mappings, backfill_history must never touch it - only the one
    # destination named in the call.
    monkeypatch.setattr(bridge_module, "_HISTORY_BACKFILL_PACING", 0)
    coordinator, channel_mappings, *_ = coordinator_parts
    from tests.bridge.conftest import _link

    await _link(channel_mappings, "group-1", "discord", "100")
    await _link(channel_mappings, "group-1", "irc", "#general")
    irc_receiver = FakeReceiver("irc")
    stoat_receiver = FakeReceiver("stoat")
    coordinator.register_receiver(irc_receiver)
    coordinator.register_receiver(stoat_receiver)

    await coordinator.backfill_history(
        fetch_history=_fetch_history([_message(message_id="m1")]),
        source_channel_id="100",
        destination_connector="stoat",
        destination_channel_id="200",
        limit=None,
    )

    assert irc_receiver.received == []
    assert len(stoat_receiver.received) == 1


async def test_backfill_history_returns_a_message_when_no_receiver_registered(coordinator_parts):
    coordinator, *_ = coordinator_parts

    summary = await coordinator.backfill_history(
        fetch_history=_fetch_history([_message()]),
        source_channel_id="100",
        destination_connector="stoat",
        destination_channel_id="200",
        limit=None,
    )

    assert "no receiver registered" in summary


async def test_backfill_history_handles_an_empty_history(coordinator_parts):
    coordinator, *_ = coordinator_parts
    coordinator.register_receiver(FakeReceiver("stoat"))

    summary = await coordinator.backfill_history(
        fetch_history=_fetch_history([]),
        source_channel_id="100",
        destination_connector="stoat",
        destination_channel_id="200",
        limit=None,
    )

    assert "No history to preserve" in summary


async def test_backfill_history_returns_a_message_on_fetch_failure(coordinator_parts):
    coordinator, *_ = coordinator_parts
    coordinator.register_receiver(FakeReceiver("stoat"))

    async def raising_fetch(channel_id, limit):
        raise RuntimeError("boom")

    summary = await coordinator.backfill_history(
        fetch_history=raising_fetch,
        source_channel_id="100",
        destination_connector="stoat",
        destination_channel_id="200",
        limit=None,
    )

    assert "failed" in summary


async def test_backfill_history_skips_a_message_that_raises(coordinator_parts, monkeypatch):
    monkeypatch.setattr(bridge_module, "_HISTORY_BACKFILL_PACING", 0)
    coordinator, *_ = coordinator_parts
    stoat_receiver = FakeReceiver("stoat", raises=RuntimeError("boom"))
    coordinator.register_receiver(stoat_receiver)

    summary = await coordinator.backfill_history(
        fetch_history=_fetch_history([_message(message_id="m1"), _message(message_id="m2")]),
        source_channel_id="100",
        destination_connector="stoat",
        destination_channel_id="200",
        limit=None,
    )

    assert "0 message(s), 2 skipped" in summary


async def test_backfill_history_counts_a_partial_relay_separately(coordinator_parts, monkeypatch):
    # A PartialRelayError means some (not all) of a split message's posts
    # got through - must not be folded into the full-success "relayed" count.
    monkeypatch.setattr(bridge_module, "_HISTORY_BACKFILL_PACING", 0)
    coordinator, *_ = coordinator_parts
    stoat_receiver = FakeReceiver("stoat", raises=PartialRelayError(["native-1"], RuntimeError("boom")))
    coordinator.register_receiver(stoat_receiver)

    summary = await coordinator.backfill_history(
        fetch_history=_fetch_history([_message(message_id="m1")]),
        source_channel_id="100",
        destination_connector="stoat",
        destination_channel_id="200",
        limit=None,
    )

    assert "relayed 0 message(s), 1 partially relayed" in summary


async def test_backfill_history_stops_early_on_unsupported_relay_target(coordinator_parts, monkeypatch):
    monkeypatch.setattr(bridge_module, "_HISTORY_BACKFILL_PACING", 0)
    coordinator, *_ = coordinator_parts
    stoat_receiver = FakeReceiver("stoat", raises=UnsupportedRelayTargetError("forum channel"))
    coordinator.register_receiver(stoat_receiver)

    summary = await coordinator.backfill_history(
        fetch_history=_fetch_history(
            [_message(message_id="m1"), _message(message_id="m2"), _message(message_id="m3")]
        ),
        source_channel_id="100",
        destination_connector="stoat",
        destination_channel_id="200",
        limit=None,
    )

    assert len(stoat_receiver.received) == 1  # stopped after the first failure
    assert "stopped after 0 message(s)" in summary


async def test_backfill_history_passes_include_relayed_through_to_fetch_history(coordinator_parts):
    # /import and /export (issue #161) ask for the full history, relayed and
    # bot posts included.
    coordinator, *_ = coordinator_parts
    coordinator.register_receiver(FakeReceiver("stoat"))
    calls = []

    async def fetch(channel_id, limit, *, include_relayed=False):
        calls.append((channel_id, limit, include_relayed))
        return []

    await coordinator.backfill_history(
        fetch_history=fetch,
        source_channel_id="100",
        destination_connector="stoat",
        destination_channel_id="200",
        limit=10,
        include_relayed=True,
    )

    assert calls == [("100", 10, True)]


async def test_backfill_history_default_calls_fetch_history_without_include_relayed(coordinator_parts):
    # /mirror channel with history keeps its old two-argument call, so a
    # fetch_history that doesn't know about include_relayed still works.
    coordinator, *_ = coordinator_parts
    coordinator.register_receiver(FakeReceiver("stoat"))
    calls = []

    async def fetch(channel_id, limit):
        calls.append((channel_id, limit))
        return []

    await coordinator.backfill_history(
        fetch_history=fetch,
        source_channel_id="100",
        destination_connector="stoat",
        destination_channel_id="200",
        limit=None,
    )

    assert calls == [("100", None)]


async def test_backfill_history_transfer_between_linked_channels_posts_only_to_the_destination(
    coordinator_parts, monkeypatch
):
    # A transfer (issue #161) whose source AND destination channels both sit
    # in bridge groups with other members lands only on the one destination
    # receiver - never on the destination's own linked channels.
    monkeypatch.setattr(bridge_module, "_HISTORY_BACKFILL_PACING", 0)
    coordinator, channel_mappings, *_ = coordinator_parts
    from tests.bridge.conftest import _link

    await _link(channel_mappings, "group-src", "discord", "100")
    await _link(channel_mappings, "group-src", "irc", "#src")
    await _link(channel_mappings, "group-dst", "stoat", "200")
    await _link(channel_mappings, "group-dst", "irc", "#dst")
    irc_receiver = FakeReceiver("irc")
    stoat_receiver = FakeReceiver("stoat")
    coordinator.register_receiver(irc_receiver)
    coordinator.register_receiver(stoat_receiver)

    async def fetch(channel_id, limit, *, include_relayed=False):
        return [_message(message_id="m1"), _message(message_id="m2")]

    await coordinator.backfill_history(
        fetch_history=fetch,
        source_channel_id="100",
        destination_connector="stoat",
        destination_channel_id="200",
        limit=None,
        include_relayed=True,
    )

    assert irc_receiver.received == []
    assert [r[1] for r in stoat_receiver.received] == ["200", "200"]
