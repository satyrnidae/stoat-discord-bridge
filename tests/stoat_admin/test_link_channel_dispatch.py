from __future__ import annotations

from stoat_discord_bridge.admin_commands import LinkError
from tests.fakes.fake_stoat import FakeChannel
from tests.stoat_admin.conftest import FakeLinker, _make_ctx, _make_sender


# ---------------------------------------------------------------- _link_channel


async def test_link_channel_success():
    linker = FakeLinker()
    sender = _make_sender(linker=linker)
    ctx = _make_ctx(channel=FakeChannel(id="c1", name="general"))

    await sender._link_channel(ctx, "discord", "src-id", "dest-id")

    assert linker.link_channel_calls == [
        {
            "local_connector": "stoat",
            "local_channel_id": "c1",
            "local_channel_name": "general",
            "source": "discord",
            "source_id": "src-id",
            "destination_id": "dest-id",
        }
    ]
    assert ctx.channel.sent[0]["content"] == "linked ok"


async def test_link_channel_destination_defaults_to_none():
    linker = FakeLinker()
    sender = _make_sender(linker=linker)
    ctx = _make_ctx()

    await sender._link_channel(ctx, "discord", "src-id")

    assert linker.link_channel_calls[0]["destination_id"] is None


async def test_link_channel_without_a_configured_linker():
    sender = _make_sender(linker=None)
    ctx = _make_ctx()

    await sender._link_channel(ctx, "discord", "src-id")

    assert ctx.channel.sent[0]["content"] == "Linking isn't configured."


async def test_link_channel_reports_a_link_error():
    sender = _make_sender(linker=FakeLinker(raises=LinkError("already linked elsewhere")))
    ctx = _make_ctx()

    await sender._link_channel(ctx, "discord", "src-id")

    assert ctx.channel.sent[0]["content"] == "already linked elsewhere"




# ---------------------------------------------------------------- _linked_channels


async def test_linked_channels_reports_the_invoking_channel():
    linker = FakeLinker(list_linked_channels_summary="Linked channels:\nStoat: general (c1) (this channel)")
    sender = _make_sender(linker=linker)
    channel = FakeChannel(id="c1")
    ctx = _make_ctx(channel=channel)

    await sender._linked_channels(ctx)

    assert linker.list_linked_channels_calls == [{"local_connector": "stoat", "local_channel_id": "c1"}]
    assert channel.sent[0]["content"] == "Linked channels:\nStoat: general (c1) (this channel)"


async def test_linked_channels_without_a_configured_linker():
    sender = _make_sender(linker=None)
    ctx = _make_ctx()

    await sender._linked_channels(ctx)

    assert ctx.channel.sent[0]["content"] == "Linking isn't configured."


async def test_linked_channels_needs_no_admin_permission():
    linker = FakeLinker()
    sender = _make_sender(linker=linker)
    ctx = _make_ctx(manage_server=False)

    await sender._linked_channels(ctx)  # must not be rejected

    assert linker.list_linked_channels_calls




# ---------------------------------------------------------------- _unlink_channel


async def test_unlink_channel_defaults_to_all():
    linker = FakeLinker()
    sender = _make_sender(linker=linker)
    ctx = _make_ctx(channel=FakeChannel(id="c1"))

    await sender._unlink_channel(ctx)

    assert linker.unlink_channel_calls == [{"local_connector": "stoat", "local_channel_id": "c1", "destination": None}]
    assert ctx.channel.sent[0]["content"] == "unlinked ok"


async def test_unlink_channel_with_a_specific_destination():
    linker = FakeLinker()
    sender = _make_sender(linker=linker)
    ctx = _make_ctx(channel=FakeChannel(id="c1"))

    await sender._unlink_channel(ctx, "c1", "discord")

    assert linker.unlink_channel_calls == [{"local_connector": "stoat", "local_channel_id": "c1", "destination": "discord"}]


async def test_unlink_channel_with_a_specific_local_channel_id():
    linker = FakeLinker()
    sender = _make_sender(linker=linker)
    ctx = _make_ctx(channel=FakeChannel(id="c1"))

    await sender._unlink_channel(ctx, "other-channel", "discord")

    assert linker.unlink_channel_calls == [
        {"local_connector": "stoat", "local_channel_id": "other-channel", "destination": "discord"}
    ]


async def test_unlink_channel_without_a_configured_linker():
    sender = _make_sender(linker=None)
    ctx = _make_ctx()

    await sender._unlink_channel(ctx)

    assert ctx.channel.sent[0]["content"] == "Linking isn't configured."


async def test_unlink_channel_reports_a_link_error():
    linker = FakeLinker(raises=LinkError("this channel isn't linked to anything."))
    sender = _make_sender(linker=linker)
    ctx = _make_ctx()

    await sender._unlink_channel(ctx)

    assert ctx.channel.sent[0]["content"] == "this channel isn't linked to anything."


