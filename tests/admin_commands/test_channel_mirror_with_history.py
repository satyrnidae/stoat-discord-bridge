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
    return f"stoat_{name}"


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


async def test_mirror_channel_with_history_rejects_irc_source(fake_db):
    async def backfill(**kwargs):
        raise AssertionError("must not run - irc doesn't support history")

    linker = ChannelLinker(ChannelMappingRepository(fake_db), _history_connectors(), backfill_history=backfill)
    with pytest.raises(LinkError, match="only supported between Discord and Stoat"):
        await linker.mirror_channel(
            local_connector="irc",
            local_channel_id="#general",
            local_channel_name="general",
            destination="stoat",
            with_history=True,
        )


async def test_mirror_channel_with_history_reports_both_sides_when_neither_supports_it(fake_db):
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
    assert "IRC2" in str(exc_info.value)


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


async def test_mirror_channel_with_history_rejects_a_forum_source(fake_db):
    # issue #122 code-review catch: a Discord forum redirects mirror_channel
    # to CategoryLinker.mirror_category - there's no single channel there for
    # backfill_history to target, so with_history must raise rather than
    # silently drop the request.
    from stoat_discord_bridge.admin_commands.category import CategoryLinker
    from stoat_discord_bridge.storage.category_mappings import CategoryMappingRepository

    async def backfill(**kwargs):
        raise AssertionError("must not run - forums don't support with_history")

    async def is_forum_channel(_channel_id):
        return True

    connectors = _history_connectors()
    connectors["discord"] = ConnectorInfo(
        id="discord", label="Discord", fetch_history=_fetch_history, is_forum_channel=is_forum_channel
    )
    connectors["stoat"] = ConnectorInfo(
        id="stoat",
        label="Stoat",
        ensure_channel=_ensure_channel,
        ensure_category=_ensure_channel,
        fetch_history=_fetch_history,
    )
    channel_mappings = ChannelMappingRepository(fake_db)
    linker = ChannelLinker(channel_mappings, connectors, backfill_history=backfill)
    CategoryLinker(CategoryMappingRepository(fake_db), None, linker, connectors)

    with pytest.raises(LinkError, match="Discord forum channel"):
        await linker.mirror_channel(
            local_connector="discord",
            local_channel_id="f1",
            local_channel_name="general",
            destination="stoat",
            with_history=True,
        )


async def test_mirror_channel_with_history_rejects_irc_destination(fake_db):
    async def backfill(**kwargs):
        raise AssertionError("must not run - irc doesn't support history")

    connectors = _history_connectors()
    connectors["irc"] = ConnectorInfo(id="irc", label="IRC", ensure_channel=_ensure_channel)
    linker = ChannelLinker(ChannelMappingRepository(fake_db), connectors, backfill_history=backfill)
    with pytest.raises(LinkError, match="only supported between Discord and Stoat"):
        await linker.mirror_channel(
            local_connector="discord",
            local_channel_id="d1",
            local_channel_name="general",
            destination="irc",
            with_history=True,
        )


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
