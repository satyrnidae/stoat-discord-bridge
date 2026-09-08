import dataclasses

from stoat_discord_bridge.admin_commands import ChannelLinker, ConnectorInfo, EmoteLinker, RoleLinker
from stoat_discord_bridge.models import CustomEmoji
from stoat_discord_bridge.storage.channel_mappings import ChannelMappingRepository
from stoat_discord_bridge.storage.emoji_mappings import EmojiMappingRepository
from stoat_discord_bridge.storage.role_mappings import RoleMappingRepository


# ---------------------------------------------------------------- /mirror full refresh (issue #81)


def _const(value):
    async def _resolve(_id):
        return value

    return _resolve


def _refresh_recorder():
    calls: list[str] = []

    def make(name: str, *, raises: bool = False):
        async def refresh() -> None:
            calls.append(name)
            if raises:
                raise RuntimeError("boom")

        return refresh

    return calls, make


async def test_mirror_channel_refreshes_both_connectors_before_resolving(fake_db):
    calls, make = _refresh_recorder()

    async def ensure_channel(name, category=None, is_thread_category=False, category_parent_channel_id=None):
        # by the time ensure runs, both sides must already have been refreshed
        assert calls == ["discord", "stoat"]
        return f"stoat_{name}"

    connectors = {
        "discord": ConnectorInfo(id="discord", label="Discord", refresh=make("discord")),
        "stoat": ConnectorInfo(id="stoat", label="Stoat", ensure_channel=ensure_channel, refresh=make("stoat")),
    }
    linker = ChannelLinker(ChannelMappingRepository(fake_db), connectors)

    await linker.mirror_channel(
        local_connector="discord", local_channel_id="d1", local_channel_name="general", destination="stoat"
    )
    assert calls == ["discord", "stoat"]


async def test_mirror_channel_survives_a_raising_refresh(fake_db):
    calls, make = _refresh_recorder()

    async def ensure_channel(name, category=None, is_thread_category=False, category_parent_channel_id=None):
        return f"stoat_{name}"

    connectors = {
        "discord": ConnectorInfo(id="discord", label="Discord", refresh=make("discord", raises=True)),
        "stoat": ConnectorInfo(id="stoat", label="Stoat", ensure_channel=ensure_channel, refresh=make("stoat")),
    }
    linker = ChannelLinker(ChannelMappingRepository(fake_db), connectors)

    summary = await linker.mirror_channel(
        local_connector="discord", local_channel_id="d1", local_channel_name="general", destination="stoat"
    )
    assert "Linked Discord channel 'd1'" in summary
    assert calls == ["discord", "stoat"]  # the raise didn't stop the stoat refresh


async def test_mirror_channel_tolerates_a_connector_with_no_refresh_hook(fake_db):
    async def ensure_channel(name, category=None, is_thread_category=False, category_parent_channel_id=None):
        return f"stoat_{name}"

    connectors = {
        "discord": ConnectorInfo(id="discord", label="Discord"),  # no refresh
        "stoat": ConnectorInfo(id="stoat", label="Stoat", ensure_channel=ensure_channel),  # no refresh
    }
    linker = ChannelLinker(ChannelMappingRepository(fake_db), connectors)

    summary = await linker.mirror_channel(
        local_connector="discord", local_channel_id="d1", local_channel_name="general", destination="stoat"
    )
    assert "Linked Discord channel 'd1'" in summary


async def test_mirror_role_refreshes_both_connectors(fake_db):
    from stoat_discord_bridge.admin_commands import RoleLinker
    from stoat_discord_bridge.storage.role_mappings import RoleMappingRepository

    calls, make = _refresh_recorder()

    async def ensure_role(name):
        assert set(calls) == {"discord", "stoat"}
        return f"s_{name}"

    connectors = {
        "discord": ConnectorInfo(
            id="discord", label="Discord", resolve_role_name=_const("Mods"), refresh=make("discord")
        ),
        "stoat": ConnectorInfo(
            id="stoat", label="Stoat", resolve_role_name=_const("Mods"), ensure_role=ensure_role, refresh=make("stoat")
        ),
    }
    linker = RoleLinker(RoleMappingRepository(fake_db), connectors)

    await linker.mirror_role(local_connector="discord", local_role="r1", destination="stoat")
    assert sorted(calls) == ["discord", "stoat"]


async def test_mirror_emote_refreshes_both_connectors(fake_db):
    calls, make = _refresh_recorder()

    async def resolve_emoji(_id):
        return CustomEmoji(native_id="e1", name="blob", image_url="https://x/e1.png", animated=False)

    async def ensure_emoji(emoji):
        assert sorted(calls) == ["discord", "stoat"]
        return dataclasses.replace(emoji, native_id="s_e1")

    connectors = {
        "discord": ConnectorInfo(
            id="discord",
            label="Discord",
            resolve_emoji_name=_const("blob"),
            resolve_emoji=resolve_emoji,
            refresh=make("discord"),
        ),
        "stoat": ConnectorInfo(
            id="stoat", label="Stoat", resolve_emoji_name=_const("blob"), ensure_emoji=ensure_emoji, refresh=make("stoat")
        ),
    }
    linker = EmoteLinker(EmojiMappingRepository(fake_db), connectors)

    await linker.mirror_emote(local_connector="discord", local_emote="e1", destination="stoat")
    assert sorted(calls) == ["discord", "stoat"]


