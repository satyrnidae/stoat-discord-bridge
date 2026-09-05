from __future__ import annotations

from stoat_discord_bridge.admin_commands import LinkError
from tests.stoat_admin.conftest import FakeEmoteLinker, FakeUserLinker, _make_ctx, _make_sender


# ---------------------------------------------------------------- _link_emote


async def test_link_emote_success():
    emote_linker = FakeEmoteLinker()
    sender = _make_sender(emote_linker=emote_linker)
    ctx = _make_ctx()

    await sender._link_emote(ctx, "discord", "src-id", "local-id")

    assert emote_linker.calls == [
        {"local_connector": "stoat", "local_id": "local-id", "source": "discord", "source_id": "src-id"}
    ]
    assert ctx.channel.sent[0]["content"] == "emote linked ok"


async def test_link_emote_without_a_configured_linker():
    sender = _make_sender(emote_linker=None)
    ctx = _make_ctx()

    await sender._link_emote(ctx, "discord", "src-id", "local-id")

    assert ctx.channel.sent[0]["content"] == "Linking isn't configured."


async def test_unlink_emote_defaults_destination_to_none():
    emote_linker = FakeEmoteLinker()
    sender = _make_sender(emote_linker=emote_linker)
    ctx = _make_ctx()

    await sender._unlink_emote(ctx, "blob")

    assert emote_linker.unlink_emote_calls == [
        {"local_connector": "stoat", "local_emote": "blob", "destination": None}
    ]
    assert ctx.channel.sent[0]["content"] == "emote unlinked ok"


async def test_linked_emotes_lists_the_group():
    emote_linker = FakeEmoteLinker()
    sender = _make_sender(emote_linker=emote_linker)
    ctx = _make_ctx()

    await sender._linked_emotes(ctx)

    assert emote_linker.list_linked_emotes_calls == [
        {"local_connector": "stoat", "local_emote": None, "service": None}
    ]
    assert ctx.channel.sent[0]["content"].startswith("Linked emotes:")


async def test_mirror_emote_to_all_by_default():
    emote_linker = FakeEmoteLinker()
    sender = _make_sender(emote_linker=emote_linker)
    ctx = _make_ctx()

    await sender._mirror_emote(ctx, "blob")

    assert emote_linker.mirror_emote_all_calls == [{"local_connector": "stoat", "local_emote": "blob"}]
    assert ctx.channel.sent[0]["content"] == "emote mirrored to all ok"


async def test_mirror_emote_to_a_single_destination():
    emote_linker = FakeEmoteLinker()
    sender = _make_sender(emote_linker=emote_linker)
    ctx = _make_ctx()

    await sender._mirror_emote(ctx, "blob", "discord")

    assert emote_linker.mirror_emote_calls == [
        {"local_connector": "stoat", "local_emote": "blob", "destination": "discord", "new_name": None}
    ]


async def test_mirror_emote_to_forwards_a_new_name():
    emote_linker = FakeEmoteLinker()
    sender = _make_sender(emote_linker=emote_linker)
    ctx = _make_ctx()

    await sender._mirror_emote(ctx, "blob", "discord", "blobcat")

    assert emote_linker.mirror_emote_calls == [
        {"local_connector": "stoat", "local_emote": "blob", "destination": "discord", "new_name": "blobcat"}
    ]




# ---------------------------------------------------------------- _link_user


async def test_link_user_success():
    user_linker = FakeUserLinker()
    sender = _make_sender(user_linker=user_linker)
    ctx = _make_ctx()

    await sender._link_user(ctx, "discord", "remote-id", "local-id")

    assert user_linker.calls == [
        {"local_connector": "stoat", "local_user_id": "local-id", "source": "discord", "source_user_id": "remote-id"}
    ]
    assert ctx.channel.sent[0]["content"] == "user linked ok"


async def test_link_user_without_a_configured_linker():
    sender = _make_sender(user_linker=None)
    ctx = _make_ctx()

    await sender._link_user(ctx, "discord", "remote-id", "local-id")

    assert ctx.channel.sent[0]["content"] == "User linking isn't configured."




# ---------------------------------------------------------------- _linked_users


async def test_linked_users_with_an_argument_shows_only_that_users_link():
    user_linker = FakeUserLinker()
    sender = _make_sender(user_linker=user_linker)
    ctx = _make_ctx()

    await sender._linked_users(ctx, "01KH7TH31EBY08FTQ7YC2RC4DQ")

    assert user_linker.list_linked_users_calls == [
        {"local_connector": "stoat", "local_user_id": "01KH7TH31EBY08FTQ7YC2RC4DQ"}
    ]
    assert ctx.channel.sent[0]["content"] == "Linked users:\nDiscord: ShrinerH (216591124222050304) ↔ Stoat: shriner (01KH)"


async def test_linked_users_with_no_argument_lists_everything():
    user_linker = FakeUserLinker()
    sender = _make_sender(user_linker=user_linker)
    ctx = _make_ctx()

    await sender._linked_users(ctx)

    assert user_linker.list_linked_users_calls == [{}]


async def test_linked_users_without_a_configured_user_linker():
    sender = _make_sender(user_linker=None)
    ctx = _make_ctx()

    await sender._linked_users(ctx)

    assert ctx.channel.sent[0]["content"] == "User linking isn't configured."


async def test_linked_users_needs_no_admin_permission():
    user_linker = FakeUserLinker()
    sender = _make_sender(user_linker=user_linker)
    ctx = _make_ctx(manage_server=False)

    await sender._linked_users(ctx)  # must not be rejected

    assert user_linker.list_linked_users_calls




# ---------------------------------------------------------------- _unlink_user


async def test_unlink_user_defaults_to_all_and_self():
    user_linker = FakeUserLinker()
    sender = _make_sender(user_linker=user_linker)
    ctx = _make_ctx()

    await sender._unlink_user(ctx)

    assert user_linker.unlink_user_calls == [{"local_connector": "stoat", "local_user_id": "admin-1", "destination": None}]
    assert ctx.channel.sent[0]["content"] == "user unlinked ok"


async def test_unlink_user_with_a_specific_destination_and_target():
    user_linker = FakeUserLinker()
    sender = _make_sender(user_linker=user_linker)
    ctx = _make_ctx()

    await sender._unlink_user(ctx, "discord", "s1")

    assert user_linker.unlink_user_calls == [{"local_connector": "stoat", "local_user_id": "s1", "destination": "discord"}]


async def test_unlink_user_without_a_configured_user_linker():
    sender = _make_sender(user_linker=None)
    ctx = _make_ctx()

    await sender._unlink_user(ctx)

    assert ctx.channel.sent[0]["content"] == "User linking isn't configured."


async def test_unlink_user_reports_a_link_error():
    user_linker = FakeUserLinker(raises=LinkError("this user isn't linked to anything."))
    sender = _make_sender(user_linker=user_linker)
    ctx = _make_ctx()

    await sender._unlink_user(ctx)

    assert ctx.channel.sent[0]["content"] == "this user isn't linked to anything."


