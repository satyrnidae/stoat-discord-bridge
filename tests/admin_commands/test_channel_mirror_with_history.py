"""Tests for `/mirror channel with history` (issue #122) at the
`ChannelLinker.mirror_channel` / `mirror_channel_from` layer: validating
both sides support history, wiring the `backfill_history` hook, and
skipping the backfill on the "already synced" no-op path."""

import pytest

from stoat_discord_bridge.admin_commands import ChannelLinker, ConnectorInfo, LinkError
from stoat_discord_bridge.storage.channel_mappings import ChannelMappingRepository


async def _fetch_history(channel_id, limit):
    return []


async def _ensure_channel(name, category=None, is_thread_category=False, category_parent_channel_id=None):
    return f"stoat_{name}", True


def _history_connectors(*, backfill=None):
    return {
        "discord": ConnectorInfo(id="discord", label="Discord", fetch_history=_fetch_history),
        "stoat": ConnectorInfo(
            id="stoat", label="Stoat", ensure_channel=_ensure_channel, fetch_history=_fetch_history
        ),
        "irc": ConnectorInfo(id="irc", label="IRC"),
    }


async def test_mirror_channel_with_history_requires_a_backfill_hook_wired(fake_db):
    linker = ChannelLinker(ChannelMappingRepository(fake_db), _history_connectors())
    with pytest.raises(LinkError, match="doesn't support 'with history'"):
        await linker.mirror_channel(
            local_connector="discord",
            local_channel_id="d1",
            local_channel_name="general",
            destination="stoat",
            with_history=True,
        )


async def test_mirror_channel_with_history_rejects_local_id_all(fake_db):
    # issue #123's local_id "all" bulk-mirror and issue #122's with_history
    # backfill don't compose - a single backfill request can't fan out
    # across every enumerated channel.
    async def backfill(**kwargs):
        raise AssertionError("must not run - with_history + local_id 'all' is rejected up front")

    async def list_channels():
        raise AssertionError("must not run - rejected before enumeration")

    connectors = _history_connectors()
    connectors["discord"] = ConnectorInfo(
        id="discord", label="Discord", fetch_history=_fetch_history, list_channels=list_channels
    )
    linker = ChannelLinker(ChannelMappingRepository(fake_db), connectors, backfill_history=backfill)

    with pytest.raises(LinkError, match="with history.*can't be combined with local_id 'all'"):
        await linker.mirror_channel(
            local_connector="discord",
            local_channel_id="all",
            local_channel_name="ignored",
            destination="stoat",
            with_history=True,
        )


async def test_mirror_channel_with_history_rejects_irc_source_with_no_fetch_history(fake_db):
    # An IRC connector that hasn't wired fetch_history at all (e.g. a
    # disconnected/unconfigured one) still can't be a history source -
    # supports_history is source-only now, but it's still required.
    async def backfill(**kwargs):
        raise AssertionError("must not run - this irc connector has no fetch_history wired")

    linker = ChannelLinker(ChannelMappingRepository(fake_db), _history_connectors(), backfill_history=backfill)
    with pytest.raises(LinkError, match="'with history' isn't supported from IRC"):
        await linker.mirror_channel(
            local_connector="irc",
            local_channel_id="#general",
            local_channel_name="general",
            destination="stoat",
            with_history=True,
        )


async def test_mirror_channel_with_history_reports_only_the_source_when_it_lacks_history(fake_db):
    # Loosened destination check (issue #141): only the *source*'s support
    # matters now - a destination with no fetch_history at all (unlike IRC,
    # which now wires one) is fine, only the source's absence is reported.
    async def backfill(**kwargs):
        raise AssertionError("must not run")

    connectors = {
        "irc": ConnectorInfo(id="irc", label="IRC"),
        "irc2": ConnectorInfo(id="irc2", label="IRC2", ensure_channel=_ensure_channel),
    }
    linker = ChannelLinker(ChannelMappingRepository(fake_db), connectors, backfill_history=backfill)
    with pytest.raises(LinkError) as exc_info:
        await linker.mirror_channel(
            local_connector="irc",
            local_channel_id="#general",
            local_channel_name="general",
            destination="irc2",
            with_history=True,
        )
    assert "IRC" in str(exc_info.value)
    assert "IRC2" not in str(exc_info.value)


async def test_mirror_channel_with_history_rejects_irc_destination_without_chanhistory_configured(fake_db):
    # issue #141's IRC -> IRC decision: an IRC destination needs its own
    # extra gate (supports_history_destination) since fetch_history is a
    # source-only concept - a destination that can't preserve backfilled
    # history is refused even though the source supports it fine.
    async def backfill(**kwargs):
        raise AssertionError("must not run - irc destination has no chanhistory configured")

    connectors = _history_connectors()
    connectors["irc"] = ConnectorInfo(
        id="irc", label="IRC", ensure_channel=_ensure_channel, supports_history_destination=lambda: False
    )
    linker = ChannelLinker(ChannelMappingRepository(fake_db), connectors, backfill_history=backfill)
    with pytest.raises(LinkError, match="History is not supported/configured on the target service"):
        await linker.mirror_channel(
            local_connector="discord",
            local_channel_id="d1",
            local_channel_name="general",
            destination="irc",
            with_history=True,
        )


async def test_mirror_channel_with_history_allows_irc_destination_with_chanhistory_configured(fake_db):
    async def backfill(*, fetch_history, source_channel_id, destination_connector, destination_channel_id, limit):
        return "relayed 2 message(s)."

    connectors = _history_connectors()
    connectors["irc"] = ConnectorInfo(
        id="irc", label="IRC", ensure_channel=_ensure_channel, supports_history_destination=lambda: True
    )
    linker = ChannelLinker(ChannelMappingRepository(fake_db), connectors, backfill_history=backfill)

    summary = await linker.mirror_channel(
        local_connector="discord",
        local_channel_id="d1",
        local_channel_name="general",
        destination="irc",
        with_history=True,
    )

    assert "relayed 2 message(s)." in summary


async def test_mirror_channel_with_history_survives_a_raising_backfill_hook(fake_db):
    async def backfill(**kwargs):
        raise RuntimeError("boom")

    channel_mappings = ChannelMappingRepository(fake_db)
    linker = ChannelLinker(channel_mappings, _history_connectors(), backfill_history=backfill)

    summary = await linker.mirror_channel(
        local_connector="discord",
        local_channel_id="d1",
        local_channel_name="general",
        destination="stoat",
        with_history=True,
    )

    # the channel was still created and linked, despite the backfill blowing up
    assert "Linked Discord channel 'd1'" in summary
    assert "failed unexpectedly" in summary
    assert await channel_mappings.get_bridge_group("stoat", "stoat_general") is not None


def _forum_linker(fake_db, backfill, *, ensure_channel=_ensure_channel):
    """A Discord forum `f1` with two active threads, mirrored to Stoat."""
    from stoat_discord_bridge.admin_commands.category import CategoryLinker
    from stoat_discord_bridge.storage.category_mappings import CategoryMappingRepository, ThreadCategoryRepository

    async def is_forum_channel(channel_id):
        return channel_id == "f1"

    async def channels_in_category(category_id):
        assert category_id == "f1"
        return [("t1", "first-post"), ("t2", "second-post")]

    connectors = _history_connectors()
    connectors["discord"] = ConnectorInfo(
        id="discord",
        label="Discord",
        fetch_history=_fetch_history,
        is_forum_channel=is_forum_channel,
        channels_in_category=channels_in_category,
    )
    connectors["stoat"] = ConnectorInfo(
        id="stoat",
        label="Stoat",
        ensure_channel=ensure_channel,
        ensure_category=_ensure_channel,
        fetch_history=_fetch_history,
    )
    linker = ChannelLinker(ChannelMappingRepository(fake_db), connectors, backfill_history=backfill)
    CategoryLinker(CategoryMappingRepository(fake_db), ThreadCategoryRepository(fake_db), linker, connectors)
    return linker


async def test_mirror_channel_with_history_on_a_forum_backfills_each_thread(fake_db):
    # issue #202: one `/mirror channel ... with history` naming the forum's
    # own id backfills every active thread into its new counterpart.
    calls = []

    async def backfill(*, fetch_history, source_channel_id, destination_connector, destination_channel_id, limit):
        calls.append((source_channel_id, destination_channel_id, limit))
        return f"relayed history of {source_channel_id}."

    linker = _forum_linker(fake_db, backfill)

    summary = await linker.mirror_channel(
        local_connector="discord",
        local_channel_id="f1",
        local_channel_name="forum",
        destination="stoat",
        with_history=True,
        history_limit=10,
    )

    assert calls == [("t1", "stoat_first-post", 10), ("t2", "stoat_second-post", 10)]
    assert "relayed history of t1." in summary
    assert "relayed history of t2." in summary


async def test_mirror_channel_with_history_on_a_forum_carries_on_past_a_failed_thread(fake_db):
    calls = []

    async def backfill(*, fetch_history, source_channel_id, destination_connector, destination_channel_id, limit):
        calls.append(source_channel_id)
        return "relayed."

    async def ensure_channel(name, category=None, is_thread_category=False, category_parent_channel_id=None):
        if name == "first-post":
            raise LinkError("no room for it")
        return f"stoat_{name}", True

    linker = _forum_linker(fake_db, backfill, ensure_channel=ensure_channel)

    summary = await linker.mirror_channel(
        local_connector="discord",
        local_channel_id="f1",
        local_channel_name="forum",
        destination="stoat",
        with_history=True,
    )

    assert calls == ["t2"]
    assert "'first-post' failed to create/find a channel: no room for it" in summary


def _threaded_linker(fake_db, events, *, threads_in_channel=None, fail_thread=None):
    """A Discord text channel `d1` ('general') with threads `t1`/`t2`,
    mirrored to Stoat. `events` records every ensure_channel and backfill
    call in order."""
    threads = {"t1": "first-thread", "t2": "second-thread"}

    async def default_threads_in_channel(channel_id):
        return list(threads.items()) if channel_id == "d1" else []

    async def resolve_thread_parent(channel_id):
        return ("d1", "general") if channel_id in threads else None

    async def ensure_channel(name, category=None, is_thread_category=False, category_parent_channel_id=None):
        events.append(("ensure", name, category))
        if name == fail_thread:
            raise LinkError("no room for it")
        return f"stoat_{name}", True

    async def backfill(*, fetch_history, source_channel_id, destination_connector, destination_channel_id, limit):
        events.append(("backfill", source_channel_id, destination_channel_id, limit))
        return f"relayed history of {source_channel_id}."

    connectors = _history_connectors()
    connectors["discord"] = ConnectorInfo(
        id="discord",
        label="Discord",
        fetch_history=_fetch_history,
        resolve_thread_parent=resolve_thread_parent,
        threads_in_channel=threads_in_channel or default_threads_in_channel,
    )
    connectors["stoat"] = ConnectorInfo(
        id="stoat", label="Stoat", ensure_channel=ensure_channel, fetch_history=_fetch_history
    )
    return ChannelLinker(ChannelMappingRepository(fake_db), connectors, backfill_history=backfill)


async def _mirror_general(linker, **kwargs):
    return await linker.mirror_channel(
        local_connector="discord",
        local_channel_id="d1",
        local_channel_name="general",
        destination="stoat",
        with_history=True,
        **kwargs,
    )


async def test_mirror_channel_with_history_mirrors_and_backfills_child_threads(fake_db):
    # issue #225: every thread is created before any history is copied, so a
    # thread mention in the main channel's history already resolves.
    events = []
    linker = _threaded_linker(fake_db, events)

    summary = await _mirror_general(linker, history_limit=10)

    ensures = [e for e in events if e[0] == "ensure"]
    assert [e[1] for e in ensures] == ["general", "first-thread", "second-thread"]
    assert all(e[2] == "🧵 #general" for e in ensures[1:])
    assert events[3:] == [
        ("backfill", "d1", "stoat_general", 10),
        ("backfill", "t1", "stoat_first-thread", 10),
        ("backfill", "t2", "stoat_second-thread", 10),
    ]
    for source in ("d1", "t1", "t2"):
        assert f"relayed history of {source}." in summary


async def test_mirror_channel_with_history_skips_an_already_linked_thread(fake_db):
    events = []
    linker = _threaded_linker(fake_db, events)
    await linker.mirror_channel(
        local_connector="discord", local_channel_id="t1", local_channel_name="first-thread", destination="stoat"
    )
    events.clear()

    summary = await _mirror_general(linker)

    assert [e[1] for e in events if e[0] == "backfill"] == ["d1", "t2"]
    assert "'first-thread' already synced - skipped." in summary


async def test_mirror_channel_with_history_backfills_only_the_main_channel_when_threads_cant_be_listed(fake_db):
    async def threads_in_channel(channel_id):
        raise RuntimeError("boom")

    events = []
    linker = _threaded_linker(fake_db, events, threads_in_channel=threads_in_channel)

    summary = await _mirror_general(linker)

    assert [e[1] for e in events if e[0] == "backfill"] == ["d1"]
    assert "relayed history of d1." in summary


async def test_mirror_channel_with_history_carries_on_past_a_failed_thread(fake_db):
    events = []
    linker = _threaded_linker(fake_db, events, fail_thread="first-thread")

    summary = await _mirror_general(linker)

    assert [e[1] for e in events if e[0] == "backfill"] == ["d1", "t2"]
    assert "'first-thread' failed to create/find a channel: no room for it" in summary


async def test_mirror_channel_without_history_leaves_child_threads_alone(fake_db):
    events = []
    linker = _threaded_linker(fake_db, events)

    await linker.mirror_channel(
        local_connector="discord", local_channel_id="d1", local_channel_name="general", destination="stoat"
    )

    assert events == [("ensure", "general", None)]


async def test_mirror_channel_with_history_allows_irc_destination_with_no_extra_check_wired(fake_db):
    # An IRC (or any) destination with no supports_history_destination hook
    # at all imposes no extra restriction - only IRC wires that hook, and
    # only when it wants to gate on chanhistory being configured.
    calls = []

    async def backfill(*, fetch_history, source_channel_id, destination_connector, destination_channel_id, limit):
        calls.append(destination_channel_id)
        return "relayed 1 message(s)."

    connectors = _history_connectors()
    connectors["irc"] = ConnectorInfo(id="irc", label="IRC", ensure_channel=_ensure_channel)
    linker = ChannelLinker(ChannelMappingRepository(fake_db), connectors, backfill_history=backfill)

    summary = await linker.mirror_channel(
        local_connector="discord",
        local_channel_id="d1",
        local_channel_name="general",
        destination="irc",
        with_history=True,
    )

    assert calls == ["stoat_general"]
    assert "relayed 1 message(s)." in summary


async def test_mirror_channel_with_history_invalid_limit_raises_before_creating(fake_db):
    async def ensure_channel(*args, **kwargs):
        raise AssertionError("must not run - history_limit is invalid")

    async def backfill(**kwargs):
        raise AssertionError("must not run - history_limit is invalid")

    connectors = _history_connectors()
    connectors["stoat"] = ConnectorInfo(
        id="stoat", label="Stoat", ensure_channel=ensure_channel, fetch_history=_fetch_history
    )
    linker = ChannelLinker(ChannelMappingRepository(fake_db), connectors, backfill_history=backfill)
    with pytest.raises(LinkError, match="invalid history limit"):
        await linker.mirror_channel(
            local_connector="discord",
            local_channel_id="d1",
            local_channel_name="general",
            destination="stoat",
            with_history=True,
            history_limit="soon",
        )


async def test_mirror_channel_with_history_triggers_a_backfill_on_a_fresh_link(fake_db):
    calls = []

    async def backfill(*, fetch_history, source_channel_id, destination_connector, destination_channel_id, limit):
        calls.append((fetch_history, source_channel_id, destination_connector, destination_channel_id, limit))
        return "relayed 3 message(s)."

    linker = ChannelLinker(
        ChannelMappingRepository(fake_db), _history_connectors(), backfill_history=backfill
    )

    summary = await linker.mirror_channel(
        local_connector="discord",
        local_channel_id="d1",
        local_channel_name="general",
        destination="stoat",
        with_history=True,
    )

    assert len(calls) == 1
    fetch_history, source_channel_id, destination_connector, destination_channel_id, limit = calls[0]
    assert fetch_history is _fetch_history
    assert source_channel_id == "d1"
    assert destination_connector == "stoat"
    assert destination_channel_id == "stoat_general"
    assert limit == 50  # default history_limit
    assert "relayed 3 message(s)." in summary
    assert "Linked Discord channel 'd1'" in summary


async def test_mirror_channel_with_history_all_mode_passes_none_limit(fake_db):
    calls = []

    async def backfill(*, fetch_history, source_channel_id, destination_connector, destination_channel_id, limit):
        calls.append(limit)
        return "relayed everything."

    linker = ChannelLinker(
        ChannelMappingRepository(fake_db), _history_connectors(), backfill_history=backfill
    )

    await linker.mirror_channel(
        local_connector="discord",
        local_channel_id="d1",
        local_channel_name="general",
        destination="stoat",
        with_history=True,
        history_limit="all",
    )

    assert calls == [None]


async def test_mirror_channel_with_history_skipped_when_already_synced(fake_db):
    backfill_calls = []

    async def backfill(**kwargs):
        backfill_calls.append(kwargs)
        return "relayed 1 message(s)."

    channel_mappings = ChannelMappingRepository(fake_db)
    linker = ChannelLinker(channel_mappings, _history_connectors(), backfill_history=backfill)

    # first mirror creates the link (no history requested)
    await linker.mirror_channel(
        local_connector="discord", local_channel_id="d1", local_channel_name="general", destination="stoat"
    )
    assert backfill_calls == []

    # second mirror, now with_history=True, hits the "already synced" early
    # return - must not trigger a backfill.
    summary = await linker.mirror_channel(
        local_connector="discord",
        local_channel_id="d1",
        local_channel_name="general",
        destination="stoat",
        with_history=True,
    )

    assert "already synced" in summary
    assert backfill_calls == []


async def test_mirror_channel_from_forwards_with_history(fake_db):
    calls = []

    async def backfill(*, fetch_history, source_channel_id, destination_connector, destination_channel_id, limit):
        calls.append((source_channel_id, destination_connector, destination_channel_id, limit))
        return "relayed 5 message(s)."

    connectors = {
        "discord": ConnectorInfo(
            id="discord", label="Discord", ensure_channel=_ensure_channel, fetch_history=_fetch_history
        ),
        "stoat": ConnectorInfo(id="stoat", label="Stoat", fetch_history=_fetch_history),
    }
    linker = ChannelLinker(ChannelMappingRepository(fake_db), connectors, backfill_history=backfill)

    summary = await linker.mirror_channel_from(
        local_connector="discord",
        source="stoat",
        source_id="s1",
        with_history=True,
        history_limit=10,
    )

    assert len(calls) == 1
    source_channel_id, destination_connector, destination_channel_id, limit = calls[0]
    assert source_channel_id == "s1"
    assert destination_connector == "discord"
    assert limit == 10
    assert "relayed 5 message(s)." in summary
