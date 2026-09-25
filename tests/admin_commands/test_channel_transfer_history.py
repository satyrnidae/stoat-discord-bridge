"""Tests for `/import` / `/export` (issue #161) at the
`ChannelLinker.transfer_history` layer: picking source/destination from the
direction, channel resolution, the shared history gates, the source-visibility
check, and sharing `MirrorGuard` with `/mirror`."""

import asyncio

import pytest

from stoat_discord_bridge.admin_commands import ChannelLinker, ConnectorInfo, LinkError, MirrorInProgressError
from stoat_discord_bridge.storage.channel_mappings import ChannelMappingRepository

_NAMES = {
    "discord": {"d1": "general", "d2": "random"},
    "stoat": {"s1": "lobby"},
}


async def _fetch_history(channel_id, limit, *, include_relayed=False):
    return []


def _names_for(connector):
    async def resolve_channel_name(channel_id):
        return _NAMES[connector].get(channel_id)

    async def resolve_channel_id_by_name(token):
        return next((cid for cid, name in _NAMES[connector].items() if name == token), None)

    return {"resolve_channel_name": resolve_channel_name, "resolve_channel_id_by_name": resolve_channel_id_by_name}


def _connectors(**overrides):
    base = {
        "discord": ConnectorInfo(id="discord", label="Discord", fetch_history=_fetch_history, **_names_for("discord")),
        "stoat": ConnectorInfo(id="stoat", label="Stoat", fetch_history=_fetch_history, **_names_for("stoat")),
        "irc": ConnectorInfo(id="irc", label="IRC"),
    }
    base.update(overrides)
    return base


class _Backfill:
    def __init__(self, result="history backfill: relayed 3 message(s)."):
        self.calls = []
        self.result = result

    async def __call__(self, **kwargs):
        self.calls.append(kwargs)
        return self.result


def _linker(fake_db, connectors=None, backfill=None):
    return ChannelLinker(
        ChannelMappingRepository(fake_db), connectors or _connectors(), backfill_history=backfill
    )


async def test_import_copies_the_external_channel_into_the_local_one(fake_db):
    backfill = _Backfill()
    linker = _linker(fake_db, backfill=backfill)

    summary = await linker.transfer_history(
        local_connector="discord",
        service="stoat",
        external_channel_id="s1",
        local_channel_id="d1",
        direction="import",
    )

    [call] = backfill.calls
    assert call["source_channel_id"] == "s1"
    assert call["destination_connector"] == "discord"
    assert call["destination_channel_id"] == "d1"
    assert call["include_relayed"] is True
    assert call["limit"] == 50
    assert call["fetch_history"] is _fetch_history
    assert summary == "Imported Stoat #lobby into #general: history backfill: relayed 3 message(s)."


async def test_export_copies_the_local_channel_into_the_external_one(fake_db):
    backfill = _Backfill()
    linker = _linker(fake_db, backfill=backfill)

    summary = await linker.transfer_history(
        local_connector="discord",
        service="stoat",
        external_channel_id="s1",
        local_channel_id="d1",
        direction="export",
    )

    [call] = backfill.calls
    assert call["source_channel_id"] == "d1"
    assert call["destination_connector"] == "stoat"
    assert call["destination_channel_id"] == "s1"
    assert call["include_relayed"] is True
    assert summary == "Exported #general to Stoat #lobby: history backfill: relayed 3 message(s)."


async def test_bare_channel_names_are_resolved(fake_db):
    backfill = _Backfill()
    linker = _linker(fake_db, backfill=backfill)

    await linker.transfer_history(
        local_connector="discord",
        service="stoat",
        external_channel_id="lobby",
        local_channel_id="general",
        direction="import",
    )

    assert backfill.calls[0]["source_channel_id"] == "s1"
    assert backfill.calls[0]["destination_channel_id"] == "d1"


async def test_irc_channel_ids_pass_through_without_a_name_hook(fake_db):
    backfill = _Backfill()
    linker = _linker(fake_db, backfill=backfill)

    summary = await linker.transfer_history(
        local_connector="discord",
        service="irc",
        external_channel_id="#chat",
        local_channel_id="d1",
        direction="export",
    )

    assert backfill.calls[0]["destination_channel_id"] == "#chat"
    assert "IRC #chat" in summary


@pytest.mark.parametrize(
    ("external", "local", "match"),
    [("nope", "d1", "Stoat.*'nope'"), ("s1", "nope", "Discord.*'nope'")],
)
async def test_unknown_channel_is_rejected(fake_db, external, local, match):
    backfill = _Backfill()
    linker = _linker(fake_db, backfill=backfill)

    with pytest.raises(LinkError, match=match):
        await linker.transfer_history(
            local_connector="discord",
            service="stoat",
            external_channel_id=external,
            local_channel_id=local,
            direction="import",
        )
    assert backfill.calls == []


async def test_unknown_service_is_rejected(fake_db):
    linker = _linker(fake_db, backfill=_Backfill())
    with pytest.raises(LinkError, match="'matrix' isn't a known connector"):
        await linker.transfer_history(
            local_connector="discord",
            service="matrix",
            external_channel_id="x",
            local_channel_id="d1",
            direction="import",
        )


async def test_same_channel_is_rejected(fake_db):
    backfill = _Backfill()
    linker = _linker(fake_db, backfill=backfill)
    with pytest.raises(LinkError, match="into itself"):
        await linker.transfer_history(
            local_connector="discord",
            service="discord",
            external_channel_id="general",
            local_channel_id="d1",
            direction="import",
        )
    assert backfill.calls == []


async def test_same_connector_transfer_between_different_channels_is_allowed(fake_db):
    backfill = _Backfill()
    linker = _linker(fake_db, backfill=backfill)

    await linker.transfer_history(
        local_connector="discord",
        service="discord",
        external_channel_id="d2",
        local_channel_id="d1",
        direction="import",
    )

    [call] = backfill.calls
    assert (call["source_channel_id"], call["destination_connector"], call["destination_channel_id"]) == (
        "d2",
        "discord",
        "d1",
    )


async def test_source_without_history_support_is_rejected(fake_db):
    backfill = _Backfill()
    connectors = _connectors(stoat=ConnectorInfo(id="stoat", label="Stoat", **_names_for("stoat")))
    linker = _linker(fake_db, connectors, backfill)
    with pytest.raises(LinkError, match="isn't supported from Stoat"):
        await linker.transfer_history(
            local_connector="discord",
            service="stoat",
            external_channel_id="s1",
            local_channel_id="d1",
            direction="import",
        )
    assert backfill.calls == []


async def test_destination_without_chanhistory_is_rejected(fake_db):
    backfill = _Backfill()
    connectors = _connectors(
        irc=ConnectorInfo(id="irc", label="IRC", supports_history_destination=lambda: False)
    )
    linker = _linker(fake_db, connectors, backfill)
    with pytest.raises(LinkError, match="History is not supported/configured on the target service"):
        await linker.transfer_history(
            local_connector="discord",
            service="irc",
            external_channel_id="#chat",
            local_channel_id="d1",
            direction="export",
        )
    assert backfill.calls == []


@pytest.mark.parametrize(("raw", "expected"), [(None, 50), ("20", 20), (5000, 1000), ("all", None), ("ALL", None)])
async def test_history_limit_is_resolved(fake_db, raw, expected):
    backfill = _Backfill()
    linker = _linker(fake_db, backfill=backfill)
    await linker.transfer_history(
        local_connector="discord",
        service="stoat",
        external_channel_id="s1",
        local_channel_id="d1",
        direction="import",
        history_limit=raw,
    )
    assert backfill.calls[0]["limit"] == expected


@pytest.mark.parametrize("raw", ["lots", "0", -3])
async def test_invalid_history_limit_is_rejected(fake_db, raw):
    backfill = _Backfill()
    linker = _linker(fake_db, backfill=backfill)
    with pytest.raises(LinkError, match="history limit"):
        await linker.transfer_history(
            local_connector="discord",
            service="stoat",
            external_channel_id="s1",
            local_channel_id="d1",
            direction="import",
            history_limit=raw,
        )
    assert backfill.calls == []


async def test_hidden_source_channel_is_rejected(fake_db):
    backfill = _Backfill()

    async def can_view_channel(channel_id):
        return False

    connectors = _connectors(
        stoat=ConnectorInfo(
            id="stoat",
            label="Stoat",
            fetch_history=_fetch_history,
            can_view_channel=can_view_channel,
            **_names_for("stoat"),
        )
    )
    linker = _linker(fake_db, connectors, backfill)
    with pytest.raises(LinkError, match="can't see channel"):
        await linker.transfer_history(
            local_connector="discord",
            service="stoat",
            external_channel_id="s1",
            local_channel_id="d1",
            direction="import",
        )
    assert backfill.calls == []


async def test_unwired_backfill_hook_is_rejected(fake_db):
    linker = _linker(fake_db)
    with pytest.raises(LinkError, match="doesn't support history transfer"):
        await linker.transfer_history(
            local_connector="discord",
            service="stoat",
            external_channel_id="s1",
            local_channel_id="d1",
            direction="import",
        )


async def test_raising_backfill_is_reported_not_raised(fake_db):
    async def backfill(**kwargs):
        raise RuntimeError("boom")

    linker = _linker(fake_db, backfill=backfill)
    summary = await linker.transfer_history(
        local_connector="discord",
        service="stoat",
        external_channel_id="s1",
        local_channel_id="d1",
        direction="import",
    )
    assert "failed unexpectedly: boom" in summary


# ---- MirrorGuard sharing


@pytest.mark.parametrize("held", ["discord", "stoat"])
async def test_transfer_is_rejected_while_a_mirror_holds_either_connector(fake_db, held):
    backfill = _Backfill()
    linker = _linker(fake_db, backfill=backfill)
    entered, release = asyncio.Event(), asyncio.Event()

    async def hold():
        with linker._guard.reserve([held], linker.connectors):
            entered.set()
            await release.wait()

    task = asyncio.create_task(hold())
    await entered.wait()
    try:
        with pytest.raises(MirrorInProgressError, match="mirror/import/export"):
            await linker.transfer_history(
                local_connector="discord",
                service="stoat",
                external_channel_id="s1",
                local_channel_id="d1",
                direction="import",
            )
    finally:
        release.set()
        await task
    assert backfill.calls == []


async def test_mirror_is_rejected_while_a_transfer_runs(fake_db):
    entered, release = asyncio.Event(), asyncio.Event()

    async def slow_backfill(**kwargs):
        entered.set()
        await release.wait()
        return "done"

    async def ensure_channel(name, **kwargs):
        return "new"

    connectors = _connectors(
        irc=ConnectorInfo(id="irc", label="IRC", ensure_channel=ensure_channel),
    )
    linker = _linker(fake_db, connectors, slow_backfill)
    task = asyncio.create_task(
        linker.transfer_history(
            local_connector="discord",
            service="stoat",
            external_channel_id="s1",
            local_channel_id="d1",
            direction="import",
        )
    )
    await entered.wait()
    try:
        # A /mirror *from* IRC *into* Stoat - Stoat is held by the transfer.
        with pytest.raises(MirrorInProgressError, match="Stoat"):
            await linker.mirror_channel(
                local_connector="irc",
                local_channel_id="#x",
                local_channel_name="x",
                destination="stoat",
            )
    finally:
        release.set()
        await task


async def test_reservation_is_released_after_success_and_after_an_error(fake_db):
    linker = _linker(fake_db, backfill=_Backfill())
    await linker.transfer_history(
        local_connector="discord",
        service="stoat",
        external_channel_id="s1",
        local_channel_id="d1",
        direction="import",
    )
    with pytest.raises(LinkError):
        await linker.transfer_history(
            local_connector="discord",
            service="stoat",
            external_channel_id="nope",
            local_channel_id="d1",
            direction="import",
        )
    # Both connectors are free again.
    with linker._guard.reserve(["discord", "stoat"], linker.connectors):
        pass
