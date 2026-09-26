import pytest

from stoat_discord_bridge.admin_commands import (
    ChannelLinker,
    LinkError,
    NothingLinkedError,
    RoleLinker,
    UserLinker,
    unlink_all,
)
from stoat_discord_bridge.storage.channel_mappings import ChannelMappingRepository
from stoat_discord_bridge.storage.role_mappings import RoleMappingRepository
from stoat_discord_bridge.storage.user_mappings import UserMappingRepository


@pytest.fixture
def linkers(fake_db, connectors):
    return {
        "channel_linker": ChannelLinker(ChannelMappingRepository(fake_db), connectors),
        "role_linker": RoleLinker(RoleMappingRepository(fake_db), connectors),
        "user_linker": UserLinker(UserMappingRepository(fake_db), connectors),
    }


async def _link_some(linkers):
    await linkers["channel_linker"].link_channel(
        local_connector="stoat", local_channel_id="s1", local_channel_name="general",
        source="discord", source_id="d1", destination_id=None,
    )
    await linkers["user_linker"].link_user(
        local_connector="stoat", local_user_id="alice", source="discord", source_user_id="111"
    )


async def test_unlink_all_runs_every_kind_and_skips_empty_ones(fake_db, connectors, linkers):
    await _link_some(linkers)

    summary = await unlink_all(local_connector="stoat", destination="all", connectors=connectors, **linkers)

    assert "Channels:\nDissolved 1 bridge group(s) on Stoat:" in summary
    assert "Users:\nDissolved 1 link group(s) on Stoat:" in summary
    assert "Roles" not in summary  # nothing linked, so no section
    assert await ChannelMappingRepository(fake_db).get_bridge_group("stoat", "s1") is None
    assert await UserMappingRepository(fake_db).get_link_group("stoat", "alice") is None


async def test_unlink_all_skips_a_linker_that_isnt_configured(connectors, linkers):
    await _link_some(linkers)

    summary = await unlink_all(
        local_connector="stoat", destination="discord", connectors=connectors,
        channel_linker=linkers["channel_linker"],
    )

    assert summary.startswith("Channels:\nUnlinked Discord from 1 bridge group(s) on Stoat:")
    assert "Users" not in summary


async def test_unlink_all_reports_a_failing_kind_without_aborting_the_rest(connectors, linkers):
    await _link_some(linkers)

    class Broken:
        async def unlink_role(self, **_kwargs):
            raise RuntimeError("boom")

    summary = await unlink_all(
        local_connector="stoat", destination="all", connectors=connectors,
        **{**linkers, "role_linker": Broken()},
    )

    assert "Roles:\nfailed - boom" in summary
    assert "Channels:\nDissolved" in summary and "Users:\nDissolved" in summary


async def test_unlink_all_with_nothing_linked_raises(connectors, linkers):
    with pytest.raises(NothingLinkedError, match="nothing on Stoat is linked"):
        await unlink_all(local_connector="stoat", destination="all", connectors=connectors, **linkers)


async def test_unlink_all_with_nothing_linked_to_that_service_raises(connectors, linkers):
    await _link_some(linkers)
    with pytest.raises(NothingLinkedError, match="nothing on Stoat is linked to IRC"):
        await unlink_all(local_connector="stoat", destination="irc", connectors=connectors, **linkers)


async def test_unlink_all_without_service_raises_and_deletes_nothing(fake_db, connectors, linkers):
    await _link_some(linkers)
    with pytest.raises(LinkError, match="explicit service"):
        await unlink_all(local_connector="stoat", destination=None, connectors=connectors, **linkers)
    assert await ChannelMappingRepository(fake_db).get_bridge_group("stoat", "s1") is not None
