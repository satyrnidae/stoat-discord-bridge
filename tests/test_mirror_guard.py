"""MirrorGuard - serializing concurrent `/mirror` runs by destination
connector so two of them can't race into duplicate channels/roles/etc
(issue #79)."""

import asyncio

import pytest

from stoat_discord_bridge.admin_commands import (
    ChannelLinker,
    ConnectorInfo,
    MirrorGuard,
    MirrorInProgressError,
    RoleLinker,
)
from stoat_discord_bridge.storage.channel_mappings import ChannelMappingRepository
from stoat_discord_bridge.storage.role_mappings import RoleMappingRepository


def _connectors(**overrides):
    base = {
        "discord": ConnectorInfo(id="discord", label="Discord"),
        "stoat": ConnectorInfo(id="stoat", label="Stoat"),
        "irc": ConnectorInfo(id="irc", label="IRC"),
    }
    base.update(overrides)
    return base


async def _hold(guard, destinations, connectors, entered, release):
    with guard.reserve(destinations, connectors):
        entered.set()
        await release.wait()


# ---- MirrorGuard, directly


async def test_guard_rejects_a_second_reservation_of_a_held_destination():
    guard = MirrorGuard()
    connectors = _connectors()
    entered, release = asyncio.Event(), asyncio.Event()
    task = asyncio.create_task(_hold(guard, ["stoat"], connectors, entered, release))
    await entered.wait()

    with pytest.raises(MirrorInProgressError, match="Stoat"):
        with guard.reserve(["stoat"], connectors):
            pass

    release.set()
    await task
    # freed once the holder exits
    with guard.reserve(["stoat"], connectors):
        pass


async def test_guard_is_reentrant_within_one_task():
    guard = MirrorGuard()
    connectors = _connectors()
    with guard.reserve(["stoat"], connectors):
        with guard.reserve(["stoat"], connectors):  # same task - no clash
            pass


async def test_guard_lets_different_destinations_run_in_parallel():
    guard = MirrorGuard()
    connectors = _connectors()
    entered, release = asyncio.Event(), asyncio.Event()
    task = asyncio.create_task(_hold(guard, ["stoat"], connectors, entered, release))
    await entered.wait()

    with guard.reserve(["irc"], connectors):  # unrelated destination - fine
        pass

    release.set()
    await task


async def test_guard_releases_its_claim_when_the_body_raises():
    guard = MirrorGuard()
    connectors = _connectors()
    with pytest.raises(RuntimeError):
        with guard.reserve(["stoat"], connectors):
            raise RuntimeError("boom")
    with guard.reserve(["stoat"], connectors):
        pass


async def test_guard_all_fanout_reserves_every_other_connector():
    guard = MirrorGuard()
    connectors = _connectors()
    entered, release = asyncio.Event(), asyncio.Event()
    task = asyncio.create_task(
        _hold(guard, ["stoat", "irc"], connectors, entered, release)
    )
    await entered.wait()

    with pytest.raises(MirrorInProgressError, match="IRC, Stoat"):
        with guard.reserve(["stoat", "irc"], connectors):
            pass

    release.set()
    await task


# ---- through the linkers


async def test_mirror_role_rejects_a_concurrent_run_into_the_same_destination(fake_db):
    started, gate = asyncio.Event(), asyncio.Event()

    async def ensure_role(name):
        started.set()
        await gate.wait()
        return "s1"

    connectors = _connectors(
        stoat=ConnectorInfo(id="stoat", label="Stoat", ensure_role=ensure_role)
    )
    linker = RoleLinker(RoleMappingRepository(fake_db), connectors)

    first = asyncio.create_task(
        linker.mirror_role(local_connector="discord", local_role="d1", destination="stoat")
    )
    await started.wait()

    with pytest.raises(MirrorInProgressError, match="Stoat"):
        await linker.mirror_role(local_connector="irc", local_role="i1", destination="stoat")

    gate.set()
    assert "Linked" in await first


async def test_mirror_role_still_allows_a_different_destination_concurrently(fake_db):
    started, gate = asyncio.Event(), asyncio.Event()

    async def slow_ensure_role(name):
        started.set()
        await gate.wait()
        return "s1"

    async def fast_ensure_role(name):
        return "i1"

    connectors = _connectors(
        stoat=ConnectorInfo(id="stoat", label="Stoat", ensure_role=slow_ensure_role),
        irc=ConnectorInfo(id="irc", label="IRC", ensure_role=fast_ensure_role),
    )
    linker = RoleLinker(RoleMappingRepository(fake_db), connectors)

    first = asyncio.create_task(
        linker.mirror_role(local_connector="discord", local_role="d1", destination="stoat")
    )
    await started.wait()

    # a mirror into IRC is untouched by the in-flight one into Stoat
    assert "Linked" in await linker.mirror_role(
        local_connector="discord", local_role="d2", destination="irc"
    )

    gate.set()
    assert "Linked" in await first


async def test_mirror_channel_all_does_not_block_itself(fake_db):
    async def ensure_channel(name, *args, **kwargs):
        return f"stoat_{name}"

    connectors = _connectors(
        stoat=ConnectorInfo(id="stoat", label="Stoat", ensure_channel=ensure_channel)
    )
    linker = ChannelLinker(ChannelMappingRepository(fake_db), connectors)

    out = await linker.mirror_channel_all(
        local_connector="discord", local_channel_id="d1", local_channel_name="general"
    )
    assert "still running" not in out
    assert "Linked" in out  # Stoat leg succeeded


async def test_mirror_role_all_is_rejected_wholesale_when_one_destination_is_busy(fake_db):
    started, gate = asyncio.Event(), asyncio.Event()
    irc_calls = []

    async def stoat_ensure_role(name):
        started.set()
        await gate.wait()
        return "s1"

    async def irc_ensure_role(name):
        irc_calls.append(name)
        return "i1"

    connectors = _connectors(
        stoat=ConnectorInfo(id="stoat", label="Stoat", ensure_role=stoat_ensure_role),
        irc=ConnectorInfo(id="irc", label="IRC", ensure_role=irc_ensure_role),
    )
    linker = RoleLinker(RoleMappingRepository(fake_db), connectors)

    # hold Stoat busy with a separate in-flight mirror
    busy = asyncio.create_task(
        linker.mirror_role(local_connector="discord", local_role="d1", destination="stoat")
    )
    await started.wait()

    # the whole `all` is rejected up front, naming the busy connector -
    # nothing is mirrored, not even the free IRC leg
    with pytest.raises(MirrorInProgressError, match="Stoat"):
        await linker.mirror_role_all(local_connector="discord", local_role="d2")
    assert irc_calls == []

    gate.set()
    await busy


async def test_one_shared_guard_makes_channel_and_role_mirror_exclude_each_other(fake_db):
    started, gate = asyncio.Event(), asyncio.Event()

    async def ensure_channel(name, *args, **kwargs):
        started.set()
        await gate.wait()
        return "c1"

    async def ensure_role(name):
        return "r1"

    guard = MirrorGuard()
    connectors = _connectors(
        stoat=ConnectorInfo(
            id="stoat", label="Stoat", ensure_channel=ensure_channel, ensure_role=ensure_role
        )
    )
    channels = ChannelLinker(ChannelMappingRepository(fake_db), connectors, guard=guard)
    roles = RoleLinker(RoleMappingRepository(fake_db), connectors, guard=guard)

    first = asyncio.create_task(
        channels.mirror_channel(
            local_connector="discord",
            local_channel_id="d1",
            local_channel_name="general",
            destination="stoat",
        )
    )
    await started.wait()

    with pytest.raises(MirrorInProgressError, match="Stoat"):
        await roles.mirror_role(local_connector="discord", local_role="r1", destination="stoat")

    gate.set()
    await first
