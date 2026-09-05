from __future__ import annotations

from stoat_discord_bridge.admin_commands import LinkError
from tests.fakes.fake_stoat import FakeCategory, FakeChannel
from tests.stoat_admin.conftest import FakeCategoryLinker, _make_ctx, _make_sender


# ---------------------------------------------------------------- _linked_categories


async def test_linked_categories_reports_the_invoking_channels_category():
    category_linker = FakeCategoryLinker()
    channel = FakeChannel(id="c1", category=FakeCategory(id="cat-1", title="Team"))
    sender = _make_sender(category_linker=category_linker)
    ctx = _make_ctx(channel=channel)

    await sender._linked_categories(ctx)

    assert category_linker.list_linked_categories_calls == [
        {"local_connector": "stoat", "local_category_id": "cat-1", "local_category": None}
    ]
    assert channel.sent[0]["content"] == "Linked categories:\nStoat: Team (cat-1) (this Category)"


async def test_linked_categories_without_a_configured_category_linker():
    sender = _make_sender(category_linker=None)
    ctx = _make_ctx()

    await sender._linked_categories(ctx)

    assert ctx.channel.sent[0]["content"] == "Category linking isn't configured."


async def test_linked_categories_when_invoking_channel_has_no_category():
    category_linker = FakeCategoryLinker()
    channel = FakeChannel(id="c1", category=None)
    sender = _make_sender(category_linker=category_linker)
    ctx = _make_ctx(channel=channel)

    await sender._linked_categories(ctx)

    assert ctx.channel.sent[0]["content"] == "This channel isn't in a Category."
    assert category_linker.list_linked_categories_calls == []


async def test_linked_categories_needs_no_admin_permission():
    category_linker = FakeCategoryLinker()
    channel = FakeChannel(id="c1", category=FakeCategory(id="cat-1", title="Team"))
    sender = _make_sender(category_linker=category_linker)
    ctx = _make_ctx(manage_server=False, channel=channel)

    await sender._linked_categories(ctx)  # must not be rejected

    assert category_linker.list_linked_categories_calls




# ---------------------------------------------------------------- _link_category


async def test_link_category_success():
    category_linker = FakeCategoryLinker()
    channel = FakeChannel(id="c1", category=FakeCategory(id="cat-1", title="Team"))
    sender = _make_sender(category_linker=category_linker)
    ctx = _make_ctx(channel=channel)

    await sender._link_category(ctx, "discord", "src-id", "dest-id")

    assert category_linker.link_category_calls == [
        {
            "local_connector": "stoat",
            "local_category_id": "cat-1",
            "local_category_name": "Team",
            "source": "discord",
            "source_id": "src-id",
            "destination_id": "dest-id",
        }
    ]
    assert channel.sent[0]["content"] == "category linked ok"


async def test_link_category_destination_defaults_to_none():
    category_linker = FakeCategoryLinker()
    channel = FakeChannel(id="c1", category=FakeCategory(id="cat-1", title="Team"))
    sender = _make_sender(category_linker=category_linker)
    ctx = _make_ctx(channel=channel)

    await sender._link_category(ctx, "discord", "src-id")

    assert category_linker.link_category_calls[0]["destination_id"] is None


async def test_link_category_without_a_configured_category_linker():
    sender = _make_sender(category_linker=None)
    ctx = _make_ctx()

    await sender._link_category(ctx, "discord", "src-id")

    assert ctx.channel.sent[0]["content"] == "Category linking isn't configured."


async def test_link_category_when_invoking_channel_has_no_category():
    category_linker = FakeCategoryLinker()
    channel = FakeChannel(id="c1", category=None)
    sender = _make_sender(category_linker=category_linker)
    ctx = _make_ctx(channel=channel)

    await sender._link_category(ctx, "discord", "src-id")

    assert ctx.channel.sent[0]["content"] == "This channel isn't in a Category."
    assert category_linker.link_category_calls == []


async def test_link_category_reports_a_link_error():
    channel = FakeChannel(id="c1", category=FakeCategory(id="cat-1", title="Team"))
    sender = _make_sender(
        category_linker=FakeCategoryLinker(raises=LinkError("that Category is used for thread mirroring"))
    )
    ctx = _make_ctx(channel=channel)

    await sender._link_category(ctx, "discord", "src-id")

    assert channel.sent[0]["content"] == "that Category is used for thread mirroring"




# ---------------------------------------------------------------- _unlink_category


async def test_unlink_category_defaults_to_all():
    category_linker = FakeCategoryLinker()
    channel = FakeChannel(id="c1", category=FakeCategory(id="cat-1", title="Team"))
    sender = _make_sender(category_linker=category_linker)
    ctx = _make_ctx(channel=channel)

    await sender._unlink_category(ctx)

    assert category_linker.unlink_category_calls == [
        {"local_connector": "stoat", "local_category_id": "cat-1", "local_category": None, "destination": None}
    ]
    assert channel.sent[0]["content"] == "category unlinked ok"


async def test_unlink_category_with_a_specific_destination():
    category_linker = FakeCategoryLinker()
    channel = FakeChannel(id="c1", category=FakeCategory(id="cat-1", title="Team"))
    sender = _make_sender(category_linker=category_linker)
    ctx = _make_ctx(channel=channel)

    await sender._unlink_category(ctx, "Team", "discord")

    assert category_linker.unlink_category_calls == [
        {"local_connector": "stoat", "local_category_id": "cat-1", "local_category": "Team", "destination": "discord"}
    ]


async def test_unlink_category_without_a_configured_category_linker():
    sender = _make_sender(category_linker=None)
    ctx = _make_ctx()

    await sender._unlink_category(ctx)

    assert ctx.channel.sent[0]["content"] == "Category linking isn't configured."


async def test_unlink_category_when_invoking_channel_has_no_category():
    category_linker = FakeCategoryLinker()
    channel = FakeChannel(id="c1", category=None)
    sender = _make_sender(category_linker=category_linker)
    ctx = _make_ctx(channel=channel)

    await sender._unlink_category(ctx)

    assert ctx.channel.sent[0]["content"] == "This channel isn't in a Category."
    assert category_linker.unlink_category_calls == []


async def test_unlink_category_reports_a_link_error():
    channel = FakeChannel(id="c1", category=FakeCategory(id="cat-1", title="Team"))
    sender = _make_sender(
        category_linker=FakeCategoryLinker(raises=LinkError("this Category isn't linked to anything."))
    )
    ctx = _make_ctx(channel=channel)

    await sender._unlink_category(ctx)

    assert channel.sent[0]["content"] == "this Category isn't linked to anything."


