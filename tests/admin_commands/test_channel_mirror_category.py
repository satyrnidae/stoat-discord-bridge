import pytest

from stoat_discord_bridge.admin_commands import ChannelLinker, ConnectorInfo, LinkError
from stoat_discord_bridge.models import ChannelMetadata
from stoat_discord_bridge.storage.category_mappings import CategoryMapping, CategoryMappingRepository
from stoat_discord_bridge.storage.channel_mappings import ChannelMappingRepository


async def test_mirror_channel_destination_category_rejects_an_unresolvable_bare_id(fake_db):
    async def ensure_channel(name, category=None, is_thread_category=False, category_parent_channel_id=None):
        return f"stoat_{name}"

    async def resolve_category_name(_cid):
        return None  # a stale/typo'd id that matches nothing

    connectors = {
        "discord": ConnectorInfo(id="discord", label="Discord"),
        "stoat": ConnectorInfo(
            id="stoat", label="Stoat", ensure_channel=ensure_channel, resolve_category_name=resolve_category_name
        ),
    }
    linker = ChannelLinker(ChannelMappingRepository(fake_db), connectors, CategoryMappingRepository(fake_db))

    with pytest.raises(LinkError, match="couldn't find a Category"):
        await linker.mirror_channel(
            local_connector="discord",
            local_channel_id="d1",
            local_channel_name="general",
            destination="stoat",
            destination_category="01J9ZXCV01J9ZXCV01J9ZXCV01",
        )


async def test_mirror_channel_destination_category_overrides_the_linked_category(fake_db):
    # issue #75: an explicit Category on the destination wins over `/link
    # category` resolution - and an id is resolved to its title so
    # ensure_channel doesn't spawn a Category named after the id.
    calls = []

    async def ensure_channel(name, category=None, is_thread_category=False, category_parent_channel_id=None):
        calls.append((name, category))
        return f"stoat_{name}"

    async def resolve_channel_category(cid):
        return ("dcat", "Discord Team")

    async def resolve_category_name(cid):
        return "Announcements" if cid == "scat-2" else None

    connectors = {
        "discord": ConnectorInfo(id="discord", label="Discord", resolve_channel_category=resolve_channel_category),
        "stoat": ConnectorInfo(
            id="stoat", label="Stoat", ensure_channel=ensure_channel, resolve_category_name=resolve_category_name
        ),
    }
    category_mappings = CategoryMappingRepository(fake_db)
    await category_mappings.upsert(
        CategoryMapping(bridge_group="g1", connector_id="discord", category_id="dcat", category_name="Discord Team")
    )
    await category_mappings.upsert(
        CategoryMapping(bridge_group="g1", connector_id="stoat", category_id="scat", category_name="Stoat Team")
    )
    linker = ChannelLinker(ChannelMappingRepository(fake_db), connectors, category_mappings)

    await linker.mirror_channel(
        local_connector="discord",
        local_channel_id="d1",
        local_channel_name="general",
        destination="stoat",
        local_channel_category="Discord Team",
        destination_category="scat-2",
    )
    assert calls == [("general", "Announcements")]


async def test_mirror_channel_destination_category_passes_an_unresolvable_name_through(fake_db):
    calls = []

    async def ensure_channel(name, category=None, is_thread_category=False, category_parent_channel_id=None):
        calls.append((name, category))
        return f"stoat_{name}"

    connectors = {
        "discord": ConnectorInfo(id="discord", label="Discord"),
        "stoat": ConnectorInfo(id="stoat", label="Stoat", ensure_channel=ensure_channel),
    }
    linker = ChannelLinker(ChannelMappingRepository(fake_db), connectors, CategoryMappingRepository(fake_db))

    await linker.mirror_channel(
        local_connector="discord",
        local_channel_id="d1",
        local_channel_name="general",
        destination="stoat",
        destination_category="Brand New Category",
    )
    assert calls == [("general", "Brand New Category")]


async def test_mirror_channel_from_local_category_overrides_the_linked_category(fake_db):
    calls = []

    async def ensure_channel(name, category=None, is_thread_category=False, category_parent_channel_id=None):
        calls.append((name, category))
        return f"stoat_{name}"

    async def resolve_channel_name(channel_id):
        return "remote-general" if channel_id == "d1" else None

    async def resolve_channel_category(cid):
        return ("dcat", "Discord Team")

    connectors = {
        "discord": ConnectorInfo(
            id="discord", label="Discord", resolve_channel_name=resolve_channel_name,
            resolve_channel_category=resolve_channel_category,
        ),
        "stoat": ConnectorInfo(id="stoat", label="Stoat", ensure_channel=ensure_channel),
    }
    category_mappings = CategoryMappingRepository(fake_db)
    await category_mappings.upsert(
        CategoryMapping(bridge_group="g1", connector_id="discord", category_id="dcat", category_name="Discord Team")
    )
    await category_mappings.upsert(
        CategoryMapping(bridge_group="g1", connector_id="stoat", category_id="scat", category_name="Stoat Team")
    )
    linker = ChannelLinker(ChannelMappingRepository(fake_db), connectors, category_mappings)

    await linker.mirror_channel_from(
        local_connector="stoat", source="discord", source_id="d1", local_category="Chosen Locally"
    )
    assert calls == [("remote-general", "Chosen Locally")]


async def test_mirror_channel_all_uses_each_destinations_linked_category(fake_db):
    calls = []

    async def ensure_channel(name, category=None, is_thread_category=False, category_parent_channel_id=None):
        calls.append((name, category))
        return f"stoat_{name}"

    async def resolve_channel_category(cid):
        return ("dcat", "Discord Team")

    connectors = {
        "discord": ConnectorInfo(id="discord", label="Discord", resolve_channel_category=resolve_channel_category),
        "stoat": ConnectorInfo(id="stoat", label="Stoat", ensure_channel=ensure_channel),
    }
    category_mappings = CategoryMappingRepository(fake_db)
    await category_mappings.upsert(
        CategoryMapping(bridge_group="g1", connector_id="discord", category_id="dcat", category_name="Discord Team")
    )
    await category_mappings.upsert(
        CategoryMapping(bridge_group="g1", connector_id="stoat", category_id="scat", category_name="Stoat Team")
    )
    linker = ChannelLinker(ChannelMappingRepository(fake_db), connectors, category_mappings)

    await linker.mirror_channel_all(
        local_connector="discord",
        local_channel_id="d1",
        local_channel_name="general",
        local_channel_category="Discord Team",
    )
    assert calls == [("general", "Stoat Team")]


async def test_mirror_channel_reads_source_metadata_and_forwards_it_to_ensure_channel(fake_db):
    ensure_calls = []

    async def describe_channel(channel_id):
        assert channel_id == "d1"
        return ChannelMetadata(description="the source topic", nsfw=True)

    async def ensure_channel(name, category=None, is_thread_category=False, category_parent_channel_id=None, *, metadata=None):
        ensure_calls.append(metadata)
        return f"stoat_{name}"

    connectors = {
        "discord": ConnectorInfo(id="discord", label="Discord", describe_channel=describe_channel),
        "stoat": ConnectorInfo(id="stoat", label="Stoat", ensure_channel=ensure_channel),
    }
    linker = ChannelLinker(ChannelMappingRepository(fake_db), connectors)

    await linker.mirror_channel(
        local_connector="discord", local_channel_id="d1", local_channel_name="general", destination="stoat"
    )
    assert ensure_calls == [ChannelMetadata(description="the source topic", nsfw=True)]


async def test_mirror_channel_omits_the_metadata_kwarg_when_the_source_has_no_describe_hook(fake_db):
    # An ensure_channel fake that doesn't accept `metadata` must still work -
    # mirror_channel only passes the kwarg when there's metadata to pass.
    seen = []

    async def ensure_channel(name, category=None, is_thread_category=False, category_parent_channel_id=None):
        seen.append(name)
        return f"stoat_{name}"

    connectors = {
        "discord": ConnectorInfo(id="discord", label="Discord"),  # no describe_channel
        "stoat": ConnectorInfo(id="stoat", label="Stoat", ensure_channel=ensure_channel),
    }
    linker = ChannelLinker(ChannelMappingRepository(fake_db), connectors)

    await linker.mirror_channel(
        local_connector="discord", local_channel_id="d1", local_channel_name="general", destination="stoat"
    )
    assert seen == ["general"]


async def test_mirror_channel_survives_a_raising_describe_channel(fake_db):
    async def describe_channel(channel_id):
        raise RuntimeError("boom")

    async def ensure_channel(name, category=None, is_thread_category=False, category_parent_channel_id=None, *, metadata=None):
        return f"stoat_{name}"

    connectors = {
        "discord": ConnectorInfo(id="discord", label="Discord", describe_channel=describe_channel),
        "stoat": ConnectorInfo(id="stoat", label="Stoat", ensure_channel=ensure_channel),
    }
    linker = ChannelLinker(ChannelMappingRepository(fake_db), connectors)

    result = await linker.mirror_channel(
        local_connector="discord", local_channel_id="d1", local_channel_name="general", destination="stoat"
    )
    assert "Linked" in result


async def test_mirror_channel_forwards_is_thread_category_to_ensure_channel(fake_db):
    calls = []

    async def ensure_channel(name, category=None, is_thread_category=False, category_parent_channel_id=None):
        calls.append((name, category, is_thread_category))
        return f"stoat_{name}"

    connectors = {
        "discord": ConnectorInfo(id="discord", label="Discord"),
        "stoat": ConnectorInfo(id="stoat", label="Stoat", ensure_channel=ensure_channel),
    }
    linker = ChannelLinker(ChannelMappingRepository(fake_db), connectors)

    await linker.mirror_channel(
        local_connector="discord",
        local_channel_id="d1",
        local_channel_name="Test Thread",
        destination="stoat",
        local_channel_category="Announcements",
        is_thread_category=True,
    )
    assert calls == [("Test Thread", "Announcements", True)]


async def test_mirror_channel_defaults_is_thread_category_to_false(fake_db):
    calls = []

    async def ensure_channel(name, category=None, is_thread_category=False, category_parent_channel_id=None):
        calls.append(is_thread_category)
        return f"stoat_{name}"

    connectors = {
        "discord": ConnectorInfo(id="discord", label="Discord"),
        "stoat": ConnectorInfo(id="stoat", label="Stoat", ensure_channel=ensure_channel),
    }
    linker = ChannelLinker(ChannelMappingRepository(fake_db), connectors)

    await linker.mirror_channel(
        local_connector="discord", local_channel_id="d1", local_channel_name="general", destination="stoat"
    )
    assert calls == [False]


async def test_mirror_channel_names_category_after_the_destinations_linked_parent(fake_db):
    calls = []

    async def ensure_channel(name, category=None, is_thread_category=False, category_parent_channel_id=None):
        calls.append((name, category))
        return f"stoat_{name}"

    async def resolve_channel_name(channel_id):
        return {"s-parent": "Bot Config"}.get(channel_id)

    connectors = {
        "discord": ConnectorInfo(id="discord", label="Discord"),
        "stoat": ConnectorInfo(
            id="stoat", label="Stoat", ensure_channel=ensure_channel, resolve_channel_name=resolve_channel_name
        ),
    }
    channel_mappings = ChannelMappingRepository(fake_db)
    linker = ChannelLinker(channel_mappings, connectors)
    await linker.link_channel(
        local_connector="discord", local_channel_id="d-parent", local_channel_name="bot-config",
        source="stoat", source_id="s-parent", destination_id=None,
    )

    await linker.mirror_channel(
        local_connector="discord", local_channel_id="d-thread", local_channel_name="cool thread",
        destination="stoat", local_channel_category="bot-config", category_from_channel_id="d-parent",
    )

    assert calls == [("cool thread", "Bot Config")]  # Stoat's own name for the parent, not "bot-config"


async def test_mirror_channel_forwards_parent_channel_id_to_ensure_channel(fake_db):
    calls = []

    async def ensure_channel(name, category=None, is_thread_category=False, category_parent_channel_id=None):
        calls.append((name, category, category_parent_channel_id))
        return f"stoat_{name}"

    async def resolve_channel_name(channel_id):
        return {"s-parent": "Bot Config"}.get(channel_id)

    connectors = {
        "discord": ConnectorInfo(id="discord", label="Discord"),
        "stoat": ConnectorInfo(
            id="stoat", label="Stoat", ensure_channel=ensure_channel, resolve_channel_name=resolve_channel_name
        ),
    }
    channel_mappings = ChannelMappingRepository(fake_db)
    linker = ChannelLinker(channel_mappings, connectors)
    await linker.link_channel(
        local_connector="discord", local_channel_id="d-parent", local_channel_name="bot-config",
        source="stoat", source_id="s-parent", destination_id=None,
    )

    await linker.mirror_channel(
        local_connector="discord", local_channel_id="d-thread", local_channel_name="cool thread",
        destination="stoat", local_channel_category="bot-config", category_from_channel_id="d-parent",
    )

    # Stoat's own channel id for the parent reaches ensure_channel, keying the binding.
    assert calls == [("cool thread", "Bot Config", "s-parent")]


async def test_mirror_channel_category_falls_back_when_parent_isnt_linked_to_destination(fake_db):
    calls = []

    async def ensure_channel(name, category=None, is_thread_category=False, category_parent_channel_id=None):
        calls.append((name, category))
        return f"stoat_{name}"

    connectors = {
        "discord": ConnectorInfo(id="discord", label="Discord"),
        "stoat": ConnectorInfo(id="stoat", label="Stoat", ensure_channel=ensure_channel),
    }
    linker = ChannelLinker(ChannelMappingRepository(fake_db), connectors)

    await linker.mirror_channel(
        local_connector="discord", local_channel_id="d-thread", local_channel_name="cool thread",
        destination="stoat", local_channel_category="bot-config", category_from_channel_id="d-parent",
    )

    assert calls == [("cool thread", "bot-config")]  # no link -> Discord parent name


async def test_mirror_channel_infers_thread_category_from_resolve_thread_parent(fake_db):
    """A manual `/mirror channel` on a Discord thread groups the counterpart
    under a Category named after the thread's parent channel - and marks it a
    thread Category - without the caller passing is_thread_category (issue #72)."""
    calls = []

    async def ensure_channel(name, category=None, is_thread_category=False, category_parent_channel_id=None):
        calls.append((name, category, is_thread_category, category_parent_channel_id))
        return f"stoat_{name}"

    async def resolve_channel_name(channel_id):
        return {"s-parent": "Bot Config"}.get(channel_id)

    async def resolve_thread_parent(channel_id):
        return ("d-parent", "bot-config") if channel_id == "d-thread" else None

    connectors = {
        "discord": ConnectorInfo(id="discord", label="Discord", resolve_thread_parent=resolve_thread_parent),
        "stoat": ConnectorInfo(
            id="stoat", label="Stoat", ensure_channel=ensure_channel, resolve_channel_name=resolve_channel_name
        ),
    }
    channel_mappings = ChannelMappingRepository(fake_db)
    linker = ChannelLinker(channel_mappings, connectors)
    await linker.link_channel(
        local_connector="discord", local_channel_id="d-parent", local_channel_name="bot-config",
        source="stoat", source_id="s-parent", destination_id=None,
    )

    await linker.mirror_channel(
        local_connector="discord", local_channel_id="d-thread", local_channel_name="cool thread",
        destination="stoat", local_channel_category="Some Other Category",
    )

    # thread Category named after (and bound to) Stoat's own copy of the parent
    assert calls == [("cool thread", "Bot Config", True, "s-parent")]


async def test_mirror_channel_thread_parent_falls_back_to_source_name_when_unlinked(fake_db):
    calls = []

    async def ensure_channel(name, category=None, is_thread_category=False, category_parent_channel_id=None):
        calls.append((name, category, is_thread_category, category_parent_channel_id))
        return f"stoat_{name}"

    async def resolve_thread_parent(channel_id):
        return ("d-parent", "bot-config")

    connectors = {
        "discord": ConnectorInfo(id="discord", label="Discord", resolve_thread_parent=resolve_thread_parent),
        "stoat": ConnectorInfo(id="stoat", label="Stoat", ensure_channel=ensure_channel),
    }
    linker = ChannelLinker(ChannelMappingRepository(fake_db), connectors)

    await linker.mirror_channel(
        local_connector="discord", local_channel_id="d-thread", local_channel_name="cool thread",
        destination="stoat",
    )

    # parent not linked on Stoat -> Category by the Discord parent name, no binding id
    assert calls == [("cool thread", "bot-config", True, None)]


async def test_mirror_channel_non_thread_ignores_resolve_thread_parent(fake_db):
    calls = []

    async def ensure_channel(name, category=None, is_thread_category=False, category_parent_channel_id=None):
        calls.append((name, category, is_thread_category))
        return f"stoat_{name}"

    async def resolve_thread_parent(channel_id):
        return None  # not a thread

    connectors = {
        "discord": ConnectorInfo(id="discord", label="Discord", resolve_thread_parent=resolve_thread_parent),
        "stoat": ConnectorInfo(id="stoat", label="Stoat", ensure_channel=ensure_channel),
    }
    linker = ChannelLinker(ChannelMappingRepository(fake_db), connectors)

    await linker.mirror_channel(
        local_connector="discord", local_channel_id="d1", local_channel_name="general",
        destination="stoat", local_channel_category="General",
    )
    assert calls == [("general", "General", False)]