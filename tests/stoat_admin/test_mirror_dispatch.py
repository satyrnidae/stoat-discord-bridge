from __future__ import annotations

from tests.fakes.fake_stoat import FakeCategory, FakeChannel, FakeClient
from tests.stoat_admin.conftest import (
    FakeCategoryLinker,
    FakeEmoteLinker,
    FakeLinker,
    FakeRoleLinker,
    _make_ctx,
    _make_sender,
)


# ---------------------------------------------------------------- _mirror_channel


async def test_mirror_channel_to_a_single_destination():
    linker = FakeLinker()
    sender = _make_sender(linker=linker)
    ctx = _make_ctx(channel=FakeChannel(id="c1", name="general"))

    await sender._mirror_channel(ctx, "general", "discord")

    assert linker.mirror_channel_calls == [
        {
            "local_connector": "stoat",
            "local_channel_id": "general",
            "local_channel_name": "general",
            "destination": "discord",
            "local_channel_category": None,
            "destination_category": None,
            "new_name": None,
        }
    ]
    assert ctx.channel.sent[0]["content"] == "mirrored ok"


async def test_mirror_channel_to_forwards_a_new_name():
    linker = FakeLinker()
    sender = _make_sender(linker=linker)
    ctx = _make_ctx(channel=FakeChannel(id="c1", name="general"))

    await sender._mirror_channel(ctx, "general", "discord", "lobby")

    assert linker.mirror_channel_calls[0]["new_name"] == "lobby"


async def test_mirror_channel_resolves_and_forwards_the_channels_category():
    linker = FakeLinker()
    channel = FakeChannel(id="c1", name="general", category=FakeCategory(id="cat-1", title="Team Alpha"))
    client = FakeClient()
    client.add_channel(channel)
    sender = _make_sender(linker=linker, client=client)
    ctx = _make_ctx(channel=channel)

    await sender._mirror_channel(ctx, "c1", "discord")

    assert linker.mirror_channel_calls[0]["local_channel_category"] == "Team Alpha"


async def test_mirror_channel_to_all_is_case_insensitive():
    linker = FakeLinker()
    sender = _make_sender(linker=linker)
    ctx = _make_ctx()

    await sender._mirror_channel(ctx, "general", "ALL")

    assert linker.mirror_channel_all_calls
    assert ctx.channel.sent[0]["content"] == "mirrored to all ok"


async def test_mirror_channel_no_args_mirrors_the_current_channel_to_all():
    linker = FakeLinker()
    sender = _make_sender(linker=linker)
    ctx = _make_ctx(channel=FakeChannel(id="c1", name="general"))

    await sender._mirror_channel(ctx)

    assert linker.mirror_channel_all_calls[0]["local_channel_id"] == "c1"


async def test_mirror_channel_uses_an_explicit_channel_id_when_given():
    linker = FakeLinker()
    sender = _make_sender(linker=linker)
    ctx = _make_ctx()

    await sender._mirror_channel(ctx, "explicit-id", "discord")

    call = linker.mirror_channel_calls[0]
    assert call["local_channel_id"] == "explicit-id"
    assert call["local_channel_name"] == "explicit-id"


async def test_mirror_channel_without_a_configured_linker():
    sender = _make_sender(linker=None)
    ctx = _make_ctx()

    await sender._mirror_channel(ctx, "discord")

    assert ctx.channel.sent[0]["content"] == "Linking isn't configured."


async def test_mirror_channel_from_routes_to_the_linker():
    linker = FakeLinker()
    sender = _make_sender(linker=linker)
    ctx = _make_ctx()

    await sender._mirror_channel_from(ctx, "discord", "d1")

    assert linker.mirror_channel_from_calls == [
        {
            "local_connector": "stoat",
            "source": "discord",
            "source_id": "d1",
            "new_name": None,
            "local_category": None,
        }
    ]
    assert ctx.channel.sent[0]["content"] == "mirrored from ok"


async def test_mirror_channel_from_forwards_a_new_name():
    linker = FakeLinker()
    sender = _make_sender(linker=linker)
    ctx = _make_ctx()

    await sender._mirror_channel_from(ctx, "discord", "d1", "lobby")

    assert linker.mirror_channel_from_calls == [
        {
            "local_connector": "stoat",
            "source": "discord",
            "source_id": "d1",
            "new_name": "lobby",
            "local_category": None,
        }
    ]


async def test_mirror_channel_forwards_the_destination_category():
    linker = FakeLinker()
    sender = _make_sender(linker=linker)
    ctx = _make_ctx(channel=FakeChannel(id="c1", name="general"))

    await sender._mirror_channel(ctx, "general", "discord", None, "Announcements")

    assert linker.mirror_channel_calls[0]["destination_category"] == "Announcements"


async def test_mirror_channel_rejects_a_category_with_all():
    linker = FakeLinker()
    sender = _make_sender(linker=linker)
    ctx = _make_ctx(channel=FakeChannel(id="c1", name="general"))

    await sender._mirror_channel(ctx, "general", "all", None, "Announcements")

    assert linker.mirror_channel_calls == []
    assert linker.mirror_channel_all_calls == []
    assert "single service" in ctx.channel.sent[0]["content"]


async def test_mirror_channel_from_forwards_the_local_category():
    linker = FakeLinker()
    sender = _make_sender(linker=linker)
    ctx = _make_ctx()

    await sender._mirror_channel_from(ctx, "discord", "d1", None, "Team Beta")

    assert linker.mirror_channel_from_calls[0]["local_category"] == "Team Beta"


async def test_mirror_role_from_routes_to_the_role_linker():
    role_linker = FakeRoleLinker()
    sender = _make_sender(role_linker=role_linker)
    ctx = _make_ctx()

    await sender._mirror_role_from(ctx, "discord", "Mods")

    assert role_linker.mirror_role_from_calls == [
        {"local_connector": "stoat", "source": "discord", "source_role": "Mods", "new_name": None}
    ]


async def test_mirror_emote_from_routes_to_the_emote_linker():
    emote_linker = FakeEmoteLinker()
    sender = _make_sender(emote_linker=emote_linker)
    ctx = _make_ctx()

    await sender._mirror_emote_from(ctx, "discord", "blob")

    assert emote_linker.mirror_emote_from_calls == [
        {"local_connector": "stoat", "source": "discord", "source_emote": "blob", "new_name": None}
    ]


async def test_mirror_category_from_routes_to_the_category_linker():
    category_linker = FakeCategoryLinker()
    sender = _make_sender(category_linker=category_linker)
    ctx = _make_ctx()

    await sender._mirror_category_from(ctx, "discord", "d-cat")

    assert category_linker.mirror_category_from_calls == [
        {"local_connector": "stoat", "source": "discord", "source_id": "d-cat", "new_name": None}
    ]


