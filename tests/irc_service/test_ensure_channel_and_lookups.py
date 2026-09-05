from __future__ import annotations

from tests.irc_service.conftest import FakeConnection, _make_sender, _patch_connection


# ---------------------------------------------------------------- ensure_channel


async def test_ensure_channel_adds_hash_prefix_if_missing(monkeypatch):
    sender = _make_sender()
    conn = FakeConnection()
    _patch_connection(monkeypatch, sender, conn)

    result = await sender.ensure_channel("general")

    assert result == "#general"
    assert conn.join_calls == ["#general"]


async def test_ensure_channel_leaves_existing_hash_prefix_alone(monkeypatch):
    sender = _make_sender()
    conn = FakeConnection()
    _patch_connection(monkeypatch, sender, conn)

    result = await sender.ensure_channel("#general")

    assert result == "#general"
    assert conn.join_calls == ["#general"]


async def test_ensure_channel_lowercases_and_hyphenates_a_thread_style_name(monkeypatch):
    # Discord thread names can have spaces/capitals (unlike a regular,
    # already-kebab-case Discord channel name) - IRC channel names can't
    # contain spaces, so ensure_channel has to normalize those itself.
    sender = _make_sender()
    conn = FakeConnection()
    _patch_connection(monkeypatch, sender, conn)

    result = await sender.ensure_channel("Test Thread")

    assert result == "#test-thread"
    assert conn.join_calls == ["#test-thread"]


async def test_ensure_channel_strips_characters_irc_channel_names_cant_hold(monkeypatch):
    sender = _make_sender()
    conn = FakeConnection()
    _patch_connection(monkeypatch, sender, conn)

    result = await sender.ensure_channel("gen,er:al")

    assert result == "#general"
    assert conn.join_calls == ["#general"]


async def test_ensure_channel_sets_the_topic_from_metadata_on_a_freshly_created_channel(monkeypatch):
    from stoat_discord_bridge.models import ChannelMetadata

    sender = _make_sender()
    conn = FakeConnection()
    _patch_connection(monkeypatch, sender, conn)

    await sender.ensure_channel("general", metadata=ChannelMetadata(description="the source topic", nsfw=True))

    assert conn.join_calls == ["#general"]
    assert conn.topic_calls == [("#general", "the source topic")]  # NSFW has no IRC equivalent, ignored


async def test_ensure_channel_does_not_set_topic_when_the_channel_already_existed(monkeypatch):
    from stoat_discord_bridge.models import ChannelMetadata

    sender = _make_sender()
    conn = FakeConnection()
    _patch_connection(monkeypatch, sender, conn)
    sender._channels.append("#general")  # already joined/known

    await sender.ensure_channel("general", metadata=ChannelMetadata(description="topic"))

    assert conn.topic_calls == []


# ---------------------------------------------------------- resolve_channel_id_by_name (issue #41)


async def test_resolve_channel_id_by_name_adds_hash_prefix():
    sender = _make_sender()

    assert await sender.resolve_channel_id_by_name("general") == "#general"


async def test_resolve_channel_id_by_name_leaves_existing_prefix_alone():
    sender = _make_sender()

    assert await sender.resolve_channel_id_by_name("#general") == "#general"
    assert await sender.resolve_channel_id_by_name("&local") == "&local"


async def test_resolve_channel_id_by_name_sterilizes_input():
    sender = _make_sender()

    # spaces -> hyphens, illegal `,`/`:` dropped, lowercased
    assert await sender.resolve_channel_id_by_name("My Cool, Channel") == "#my-cool-channel"


# ---------------------------------------------------------- normalize_channel_name (issue #51)


async def test_normalize_channel_name_adds_hash_prefix():
    sender = _make_sender()

    assert sender.normalize_channel_name("danksquad") == "#danksquad"
    assert sender.normalize_channel_name("#danksquad") == "#danksquad"


# ---------------------------------------------------------- list_channels (issue #41)


async def test_list_channels_offers_known_channels():
    sender = _make_sender()
    sender._channels = ["#general", "#random"]

    assert await sender.list_channels() == [("#general", "general"), ("#random", "random")]


