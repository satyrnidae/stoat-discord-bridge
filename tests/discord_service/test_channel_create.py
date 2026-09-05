from __future__ import annotations

from types import SimpleNamespace

from tests.discord_service.conftest import FakeCategoryLinker, FakeLinker, _make_sender
from tests.fakes.fake_discord import FakeGuild, FakeGuildChannel


# ---------------------------------------------------------------- _handle_channel_create


async def test_handle_channel_create_syncs_a_new_channel_in_a_linked_category():
    category_linker = FakeCategoryLinker()
    sender = _make_sender(FakeLinker(), category_linker=category_linker)
    guild = FakeGuild(id=123)
    category = FakeGuildChannel(id=555, name="Team", guild=guild)
    channel = FakeGuildChannel(id=888, name="general-2", guild=guild, category=category)

    await sender._handle_channel_create(channel)

    assert category_linker.sync_new_channel_calls == [
        {
            "local_connector": "discord",
            "local_category_id": "555",
            "channel_id": "888",
            "channel_name": "general-2",
        }
    ]


async def test_handle_channel_create_noop_without_a_configured_category_linker():
    sender = _make_sender(FakeLinker(), category_linker=None)
    guild = FakeGuild(id=123)
    category = FakeGuildChannel(id=555, name="Team", guild=guild)
    channel = FakeGuildChannel(id=888, name="general-2", guild=guild, category=category)

    await sender._handle_channel_create(channel)  # would raise if it tried to use a None category_linker


async def test_handle_channel_create_noop_for_a_channel_outside_the_configured_guild():
    category_linker = FakeCategoryLinker()
    sender = _make_sender(FakeLinker(), category_linker=category_linker)
    guild = FakeGuild(id=999)  # not this sender's configured guild_id (123)
    category = FakeGuildChannel(id=555, name="Team", guild=guild)
    channel = FakeGuildChannel(id=888, name="general-2", guild=guild, category=category)

    await sender._handle_channel_create(channel)

    assert category_linker.sync_new_channel_calls == []


async def test_handle_channel_create_noop_for_a_channel_with_no_category():
    category_linker = FakeCategoryLinker()
    sender = _make_sender(FakeLinker(), category_linker=category_linker)
    guild = FakeGuild(id=123)
    channel = FakeGuildChannel(id=888, name="general-2", guild=guild, category=None)

    await sender._handle_channel_create(channel)

    assert category_linker.sync_new_channel_calls == []


async def test_handle_channel_create_noop_for_a_non_text_or_voice_channel():
    category_linker = FakeCategoryLinker()
    sender = _make_sender(FakeLinker(), category_linker=category_linker)
    guild = FakeGuild(id=123)
    category = FakeGuildChannel(id=555, name="Team", guild=guild)
    not_a_channel = SimpleNamespace(id=888, name="whatever", guild=guild, category=category)

    await sender._handle_channel_create(not_a_channel)

    assert category_linker.sync_new_channel_calls == []



# ---------------------------------------------------------------- describe_channel / ensure_channel (issue #32)


async def test_describe_channel_reads_topic_and_nsfw(monkeypatch):
    from tests.fakes.fake_discord import FakeClient as _FakeDiscordClient

    sender = _make_sender(FakeLinker())
    channel = FakeGuildChannel(id=888, name="general", guild=FakeGuild(id=123))
    channel.topic = "the topic"
    channel.nsfw = True
    client = _FakeDiscordClient()
    client.add_channel(channel)
    monkeypatch.setattr(sender, "_client", client)

    meta = await sender.describe_channel("888")

    assert meta.description == "the topic"
    assert meta.nsfw is True
    assert meta.icon_url is None  # Discord guild text channels have no icon


async def test_ensure_channel_creates_a_text_channel_with_the_source_metadata(monkeypatch):
    sender = _make_sender(FakeLinker())
    guild = FakeGuild(id=123)
    monkeypatch.setattr(sender, "_guild_or_none", lambda: guild)

    from stoat_discord_bridge.models import ChannelMetadata

    new_id = await sender.ensure_channel(
        "general", "Team", metadata=ChannelMetadata(description="carried over", nsfw=True)
    )

    [created] = guild.created_text_channels
    assert created["name"] == "general"
    assert created["topic"] == "carried over"
    assert created["nsfw"] is True
    assert guild.created_categories == ["Team"]
    assert new_id == str(guild.text_channels[0].id)


async def test_ensure_channel_matches_an_existing_channel_and_skips_metadata(monkeypatch):
    sender = _make_sender(FakeLinker())
    guild = FakeGuild(id=123)
    existing = FakeGuildChannel(id=888, name="general", guild=guild)
    guild.text_channels.append(existing)
    monkeypatch.setattr(sender, "_guild_or_none", lambda: guild)

    from stoat_discord_bridge.models import ChannelMetadata

    new_id = await sender.ensure_channel("general", metadata=ChannelMetadata(description="ignored"))

    assert new_id == "888"
    assert guild.created_text_channels == []  # matched, nothing created
