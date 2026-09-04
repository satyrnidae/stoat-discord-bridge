"""Tests for StoatSenderService's admin-command surface - the
`_link_channel` / `_mirror_channel` / `_link_emote` / `_link_user` / … methods
the `stoat.ext.commands` tree on `_StoatClient` forwards to, and the `_is_admin`
Manage-Server gate behind the mutating ones.

Constructs the service via object.__new__, same rationale as
test_stoat_resolve_avatar.py/test_stoat_sender_dispatch.py: __init__ builds
a _StoatClient whose constructor makes a real network call that none of
these handlers need.

Argument arity / "missing required argument" is the command framework's job
now (see test_stoat_command_tree.py for the group/subcommand wiring), so
there are no "wrong arg count sends usage" tests here any more.
"""

from __future__ import annotations

from collections import deque
from types import SimpleNamespace

import stoat

from stoat_discord_bridge.admin_commands import LinkError
from stoat_discord_bridge.models import ChannelMetadata
from stoat_discord_bridge.services.stoat_service import StoatSenderService
from tests.fakes.fake_stoat import FakeAsset, FakeCategory, FakeChannel, FakeClient, FakeServer


class FakeLinker:
    def __init__(self, *, raises: LinkError | None = None) -> None:
        self._raises = raises
        self.link_channel_calls: list[dict] = []
        self.mirror_channel_calls: list[dict] = []
        self.mirror_channel_all_calls: list[dict] = []
        self.mirror_channel_from_calls: list[dict] = []
        self.list_linked_channels_calls: list[dict] = []
        self.unlink_channel_calls: list[dict] = []

    async def link_channel(self, **kwargs):
        self.link_channel_calls.append(kwargs)
        if self._raises is not None:
            raise self._raises
        return "linked ok"

    async def list_linked_channels(self, **kwargs):
        self.list_linked_channels_calls.append(kwargs)
        return "Linked channels:\nStoat: general (c1) (this channel)"

    async def mirror_channel(self, **kwargs):
        self.mirror_channel_calls.append(kwargs)
        if self._raises is not None:
            raise self._raises
        return "mirrored ok"

    async def mirror_channel_all(self, **kwargs):
        self.mirror_channel_all_calls.append(kwargs)
        if self._raises is not None:
            raise self._raises
        return "mirrored to all ok"

    async def mirror_channel_from(self, **kwargs):
        self.mirror_channel_from_calls.append(kwargs)
        if self._raises is not None:
            raise self._raises
        return "mirrored from ok"

    async def unlink_channel(self, **kwargs):
        self.unlink_channel_calls.append(kwargs)
        if self._raises is not None:
            raise self._raises
        return "unlinked ok"


class FakeEmoteLinker:
    def __init__(self, *, raises: LinkError | None = None) -> None:
        self._raises = raises
        self.calls: list[dict] = []
        self.unlink_emote_calls: list[dict] = []
        self.list_linked_emotes_calls: list[dict] = []
        self.mirror_emote_calls: list[dict] = []
        self.mirror_emote_all_calls: list[dict] = []
        self.mirror_emote_from_calls: list[dict] = []

    async def link_emote(self, **kwargs):
        self.calls.append(kwargs)
        if self._raises is not None:
            raise self._raises
        return "emote linked ok"

    async def unlink_emote(self, **kwargs):
        self.unlink_emote_calls.append(kwargs)
        if self._raises is not None:
            raise self._raises
        return "emote unlinked ok"

    async def list_linked_emotes(self, **kwargs):
        self.list_linked_emotes_calls.append(kwargs)
        return "Linked emotes:\nDiscord: blob ↔ Stoat: blob"

    async def mirror_emote(self, **kwargs):
        self.mirror_emote_calls.append(kwargs)
        if self._raises is not None:
            raise self._raises
        return "emote mirrored ok"

    async def mirror_emote_all(self, **kwargs):
        self.mirror_emote_all_calls.append(kwargs)
        if self._raises is not None:
            raise self._raises
        return "emote mirrored to all ok"

    async def mirror_emote_from(self, **kwargs):
        self.mirror_emote_from_calls.append(kwargs)
        if self._raises is not None:
            raise self._raises
        return "emote mirrored from ok"


class FakeUserLinker:
    def __init__(self, *, raises: LinkError | None = None) -> None:
        self._raises = raises
        self.calls: list[dict] = []
        self.list_linked_users_calls: list[dict] = []
        self.unlink_user_calls: list[dict] = []

    async def list_linked_users(self, **kwargs):
        self.list_linked_users_calls.append(kwargs)
        return "Linked users:\nDiscord: ShrinerH (216591124222050304) ↔ Stoat: shriner (01KH)"

    async def link_user(self, **kwargs):
        self.calls.append(kwargs)
        if self._raises is not None:
            raise self._raises
        return "user linked ok"

    async def unlink_user(self, **kwargs):
        self.unlink_user_calls.append(kwargs)
        if self._raises is not None:
            raise self._raises
        return "user unlinked ok"


class FakeCategoryLinker:
    def __init__(self, *, raises: LinkError | None = None) -> None:
        self._raises = raises
        self.link_category_calls: list[dict] = []
        self.list_linked_categories_calls: list[dict] = []
        self.unlink_category_calls: list[dict] = []
        self.sync_new_channel_calls: list[dict] = []
        self.mirror_category_calls: list[dict] = []
        self.mirror_category_all_calls: list[dict] = []
        self.mirror_category_from_calls: list[dict] = []

    async def link_category(self, **kwargs):
        self.link_category_calls.append(kwargs)
        if self._raises is not None:
            raise self._raises
        return "category linked ok"

    async def mirror_category(self, **kwargs):
        self.mirror_category_calls.append(kwargs)
        if self._raises is not None:
            raise self._raises
        return "mirrored ok"

    async def mirror_category_all(self, **kwargs):
        self.mirror_category_all_calls.append(kwargs)
        if self._raises is not None:
            raise self._raises
        return "mirrored all ok"

    async def mirror_category_from(self, **kwargs):
        self.mirror_category_from_calls.append(kwargs)
        if self._raises is not None:
            raise self._raises
        return "mirrored from ok"

    async def list_linked_categories(self, **kwargs):
        self.list_linked_categories_calls.append(kwargs)
        return "Linked categories:\nStoat: Team (cat-1) (this Category)"

    async def unlink_category(self, **kwargs):
        self.unlink_category_calls.append(kwargs)
        if self._raises is not None:
            raise self._raises
        return "category unlinked ok"

    async def sync_new_channel(self, **kwargs):
        self.sync_new_channel_calls.append(kwargs)


class FakeRoleLinker:
    def __init__(self, *, raises: LinkError | None = None) -> None:
        self._raises = raises
        self.link_role_calls: list[dict] = []
        self.unlink_role_calls: list[dict] = []
        self.list_linked_roles_calls: list[dict] = []
        self.mirror_role_calls: list[dict] = []
        self.mirror_role_all_calls: list[dict] = []
        self.mirror_role_from_calls: list[dict] = []

    async def link_role(self, **kwargs):
        self.link_role_calls.append(kwargs)
        if self._raises is not None:
            raise self._raises
        return "role linked ok"

    async def unlink_role(self, **kwargs):
        self.unlink_role_calls.append(kwargs)
        if self._raises is not None:
            raise self._raises
        return "role unlinked ok"

    async def list_linked_roles(self, **kwargs):
        self.list_linked_roles_calls.append(kwargs)
        return "Linked roles:\nDiscord: Mods ↔ Stoat: Moderators"

    async def mirror_role(self, **kwargs):
        self.mirror_role_calls.append(kwargs)
        return "role mirrored ok"

    async def mirror_role_all(self, **kwargs):
        self.mirror_role_all_calls.append(kwargs)
        return "role mirrored to all ok"

    async def mirror_role_from(self, **kwargs):
        self.mirror_role_from_calls.append(kwargs)
        return "role mirrored from ok"


def _make_sender(
    *,
    linker: FakeLinker | None = None,
    emote_linker: FakeEmoteLinker | None = None,
    user_linker: FakeUserLinker | None = None,
    category_linker: FakeCategoryLinker | None = None,
    role_linker: "FakeRoleLinker | None" = None,
    client: FakeClient | None = None,
    server_id: str | None = "s1",
) -> StoatSenderService:
    sender = object.__new__(StoatSenderService)
    sender.connector_id = "stoat"
    sender._linker = linker
    sender._emote_linker = emote_linker
    sender._user_linker = user_linker
    sender._category_linker = category_linker
    sender._role_linker = role_linker
    sender.server_id = server_id
    sender._command_message_ids = deque(maxlen=512)
    if client is not None:
        sender._client = client
    return sender


def _admin_message(*, manage_server: bool = True, channel=None):
    channel = channel if channel is not None else FakeChannel(id="c1")
    return SimpleNamespace(
        channel=channel,
        author=SimpleNamespace(id="admin-1"),
        author_as_member=SimpleNamespace(server_permissions=SimpleNamespace(manage_server=manage_server)),
    )


class _Ctx:
    """The slice of `stoat.ext.commands.Context` the `_link_*` handlers touch:
    `.message` (for `_is_admin`), `.channel`, `.author_id`, and `.send`."""

    def __init__(self, message) -> None:
        self.message = message
        self.channel = message.channel
        self.author_id = message.author.id

    async def send(self, content):
        return await self.channel.send(content)


def _make_ctx(*, manage_server: bool = True, channel=None):
    return _Ctx(_admin_message(manage_server=manage_server, channel=channel))


# ---------------------------------------------------------------- _is_admin


def test_is_admin_true_with_manage_server():
    sender = _make_sender()
    assert sender._is_admin(_admin_message(manage_server=True)) is True


def test_is_admin_false_without_manage_server():
    sender = _make_sender()
    assert sender._is_admin(_admin_message(manage_server=False)) is False


def test_is_admin_false_when_member_info_is_unavailable():
    sender = _make_sender()
    assert sender._is_admin(SimpleNamespace(channel=FakeChannel(id="c1"))) is False


class _MemberNoPermsCache:
    """Member whose computed `server_permissions` raises (cache miss)."""

    def __init__(self, *, member_id, owner_id):
        self.id = member_id
        self._owner_id = owner_id

    def get_server(self):
        return SimpleNamespace(owner_id=self._owner_id)

    @property
    def server_permissions(self):
        raise RuntimeError("permissions cache miss")


def test_is_admin_true_for_server_owner_when_permissions_unavailable():
    sender = _make_sender()
    member = _MemberNoPermsCache(member_id="owner-1", owner_id="owner-1")
    message = SimpleNamespace(channel=FakeChannel(id="c1"), author_as_member=member)
    assert sender._is_admin(message) is True


def test_is_admin_false_for_non_owner_when_permissions_unavailable():
    sender = _make_sender()
    member = _MemberNoPermsCache(member_id="member-1", owner_id="someone-else")
    message = SimpleNamespace(channel=FakeChannel(id="c1"), author_as_member=member)
    assert sender._is_admin(message) is False


# ---------------------------------------------------------------- shared "needs admin" gate


async def test_each_admin_command_rejects_a_non_admin():
    sender = _make_sender(
        linker=FakeLinker(),
        emote_linker=FakeEmoteLinker(),
        user_linker=FakeUserLinker(),
        category_linker=FakeCategoryLinker(),
        role_linker=FakeRoleLinker(),
    )
    ctx = _make_ctx(manage_server=False)

    await sender._link_channel(ctx, "discord", "s1")
    await sender._link_emote(ctx, "discord", "s1", "l1")
    await sender._link_user(ctx, "discord", "u1", "l1")
    await sender._mirror_channel(ctx)
    await sender._unlink_channel(ctx)
    await sender._unlink_user(ctx)
    await sender._link_category(ctx, "discord", "s1")
    await sender._unlink_category(ctx)
    await sender._link_role(ctx, "Mods", "discord", "111")

    assert ctx.channel.sent == [
        {"content": "You need the Manage Server permission to do that.", "masquerade": None}
    ] * 9


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


# ---------------------------------------------------------------- ensure_channel / Category placement


async def test_ensure_channel_creates_a_new_category_when_none_matches():
    server = FakeServer(id="s1")
    client = FakeClient()
    client.add_server(server)
    sender = _make_sender(client=client)

    channel_id = await sender.ensure_channel("general", "Team Alpha")

    assert channel_id == "chan-general"
    assert server.created_categories == [{"name": "Team Alpha", "channels": ["chan-general"]}]
    [category] = server.categories
    assert category.title == "Team Alpha"
    assert category.channels == ["chan-general"]


async def test_ensure_channel_adds_to_an_existing_category_by_title():
    server = FakeServer(id="s1")
    server.categories.append(FakeCategory(id="cat-1", title="Team Alpha", channels=["chan-other"]))
    client = FakeClient()
    client.add_server(server)
    sender = _make_sender(client=client)

    channel_id = await sender.ensure_channel("general", "Team Alpha")

    assert channel_id == "chan-general"
    assert server.created_categories == []  # matched the existing one - no new Category created
    [category] = server.categories
    assert category.channels == ["chan-other", "chan-general"]


async def test_ensure_channel_is_idempotent_when_channel_already_in_category():
    server = FakeServer(id="s1")
    server.categories.append(FakeCategory(id="cat-1", title="Team Alpha", channels=["chan-general"]))
    channel = FakeChannel(id="chan-general", name="general")
    server.channels.append(channel)
    client = FakeClient()
    client.add_server(server)
    sender = _make_sender(client=client)

    await sender.ensure_channel("general", "Team Alpha")

    [category] = server.categories
    assert category.channels == ["chan-general"]  # not duplicated


async def test_ensure_channel_without_a_category_leaves_categories_untouched():
    server = FakeServer(id="s1")
    client = FakeClient()
    client.add_server(server)
    sender = _make_sender(client=client)

    channel_id = await sender.ensure_channel("general")

    assert channel_id == "chan-general"
    assert server.categories == []
    assert server.created_categories == []


async def test_ensure_channel_reports_channel_even_if_category_placement_fails():
    class ExplodingServer(FakeServer):
        async def create_category(self, name, *, channels):
            raise RuntimeError("category creation failed")

    server = ExplodingServer(id="s1")
    client = FakeClient()
    client.add_server(server)
    sender = _make_sender(client=client)

    channel_id = await sender.ensure_channel("general", "Team Alpha")

    assert channel_id == "chan-general"  # channel creation itself still succeeded


# ---------------------------------------------------------------- ensure_channel metadata (issue #32)


async def test_ensure_channel_applies_description_and_nsfw_when_it_creates_the_channel():
    server = FakeServer(id="s1")
    client = FakeClient()
    client.add_server(server)
    sender = _make_sender(client=client)

    await sender.ensure_channel(
        "general", metadata=ChannelMetadata(description="the general channel", nsfw=True)
    )

    assert server.created_channel_calls == [
        {"name": "general", "description": "the general channel", "nsfw": True}
    ]


async def test_ensure_channel_downloads_and_sets_the_icon_on_create(monkeypatch):
    async def fake_download(url):
        assert url == "https://cdn.example/icon.png"
        return b"icon-bytes"

    monkeypatch.setattr(
        "stoat_discord_bridge.services.stoat_service.lookups.channels._download", fake_download
    )
    server = FakeServer(id="s1")
    client = FakeClient()
    client.add_server(server)
    sender = _make_sender(client=client)

    await sender.ensure_channel(
        "general", metadata=ChannelMetadata(icon_url="https://cdn.example/icon.png")
    )

    [created] = server.channels
    assert created.edits == [{"icon": created.icon}]  # channel.edit(icon=<Upload>) fired once


async def test_ensure_channel_leaves_an_existing_channels_metadata_alone():
    server = FakeServer(id="s1")
    existing = FakeChannel(id="chan-general", name="general", description="hand-written", nsfw=False)
    server.channels.append(existing)
    client = FakeClient()
    client.add_server(server)
    sender = _make_sender(client=client)

    channel_id = await sender.ensure_channel(
        "general", metadata=ChannelMetadata(description="from the source", nsfw=True)
    )

    assert channel_id == "chan-general"
    assert server.created_channel_calls == []  # nothing created
    assert existing.description == "hand-written"  # and the match wasn't edited
    assert existing.edits == []


async def test_describe_channel_reads_description_nsfw_and_icon():
    server = FakeServer(id="s1")
    channel = FakeChannel(
        id="c1", name="general", description="a channel", nsfw=True, icon=FakeAsset("https://cdn.example/i.png")
    )
    client = FakeClient()
    client.add_channel(channel)
    client.add_server(server)
    sender = _make_sender(client=client)

    meta = await sender.describe_channel("c1")

    assert meta == ChannelMetadata(
        description="a channel", nsfw=True, icon_url="https://cdn.example/i.png"
    )


async def test_describe_channel_returns_none_for_an_unresolvable_channel():
    client = FakeClient()
    client.add_server(FakeServer(id="s1"))
    sender = _make_sender(client=client)

    assert await sender.describe_channel("nope") is None


async def test_ensure_channel_falls_back_to_server_edit_when_the_category_endpoint_404s():
    class OldStoatServer(FakeServer):
        async def create_category(self, name, *, channels):
            raise stoat.HTTPException.__new__(stoat.NotFound)  # older API: POST /servers/{id}/categories 404s

    server = OldStoatServer(id="s1")
    client = FakeClient()
    client.add_server(server)
    sender = _make_sender(client=client)

    channel_id = await sender.ensure_channel("general", "Team Alpha")

    assert channel_id == "chan-general"
    assert server.created_categories == []
    [payload] = server.server_edits
    [category] = payload["categories"]
    assert category["title"] == "Team Alpha"
    assert category["channels"] == ["chan-general"]
    assert category["id"]  # a generated id


async def test_ensure_channel_server_edit_fallback_adds_to_an_existing_category():
    class OldStoatServer(FakeServer):
        async def create_category(self, name, *, channels):
            raise stoat.HTTPException.__new__(stoat.NotFound)

        async def edit_category(self, category, *, channels):
            raise stoat.HTTPException.__new__(stoat.NotFound)

    server = OldStoatServer(id="s1")
    server.categories.append(FakeCategory(id="cat-1", title="Bot Config", channels=["chan-other"]))
    client = FakeClient()
    client.add_server(server)
    sender = _make_sender(client=client)

    await sender.ensure_channel("general", "Bot Config")

    [payload] = server.server_edits
    [category] = payload["categories"]
    assert category["id"] == "cat-1"  # reused, not recreated
    assert category["channels"] == ["chan-other", "chan-general"]


async def test_ensure_channel_retries_category_placement_against_a_refetched_server():
    attempts = []

    class FlakyServer(FakeServer):
        async def create_category(self, name, *, channels):
            attempts.append(name)
            if len(attempts) == 1:
                raise RuntimeError("stale cache: duplicate category")
            return await super().create_category(name, channels=channels)

    server = FlakyServer(id="s1")
    client = FakeClient()
    client.add_server(server)
    sender = _make_sender(client=client)

    channel_id = await sender.ensure_channel("general", "Team Alpha")

    assert channel_id == "chan-general"
    assert attempts == ["Team Alpha", "Team Alpha"]  # failed once, retried after re-fetch
    assert [c.title for c in server.categories] == ["Team Alpha"]


# ---- issue #27: the whole-server category PATCH is rebuilt from a fresh fetch,
# ---- never the (possibly stale) cached server, so it can't revert the layout


async def test_place_via_server_edit_rebuilds_from_a_freshly_fetched_server():
    # The cache still shows the category layout from gateway-connect time; the
    # real server has gained a whole Category since. PATCHing the stale
    # snapshot straight back would delete that Category server-side (issue #27).
    stale = FakeServer(id="s1")
    stale.categories = [FakeCategory(id="cat-a", title="Alpha", channels=["ch-1"])]
    fresh = FakeServer(id="s1")
    fresh.categories = [
        FakeCategory(id="cat-a", title="Alpha", channels=["ch-1"]),
        FakeCategory(id="cat-b", title="Beta", channels=["ch-2"]),
    ]
    client = FakeClient()
    client.add_server(stale)
    client.set_fetched_server(fresh)
    sender = _make_sender(client=client)

    await sender._place_via_server_edit(stale, "ch-new", "Alpha")

    [payload] = fresh.server_edits
    cats = {c["id"]: c["channels"] for c in payload["categories"]}
    assert cats == {"cat-a": ["ch-1", "ch-new"], "cat-b": ["ch-2"]}  # Beta not dropped


async def test_place_via_server_edit_reuses_a_linked_category_absent_from_the_cache():
    # The linked Category was created after startup, so the cache lacks it;
    # matching against the stale list would miss it and mint a fresh-id
    # Category, orphaning the /link-category mapping (issue #27).
    stale = FakeServer(id="s1")  # nothing cached
    fresh = FakeServer(id="s1")
    fresh.categories = [FakeCategory(id="cat-real", title="Team", channels=["ch-1"])]
    client = FakeClient()
    client.add_server(stale)
    client.set_fetched_server(fresh)
    sender = _make_sender(client=client)

    resolved = await sender._place_via_server_edit(stale, "ch-new", "Team")

    assert resolved.id == "cat-real"  # reused, not recreated under a new id
    [payload] = fresh.server_edits
    assert [c["id"] for c in payload["categories"]] == ["cat-real"]
    assert payload["categories"][0]["channels"] == ["ch-1", "ch-new"]


async def test_place_via_server_edit_preserves_untouched_category_permissions():
    class RichCategory:
        def __init__(self, id, title, channels, extra):
            self.id, self.title, self.channels, self._extra = id, title, channels, extra

        def to_dict(self):
            return {"id": self.id, "title": self.title, "channels": list(self.channels), **self._extra}

    fresh = FakeServer(id="s1")
    fresh.categories = [
        RichCategory("cat-a", "Alpha", ["ch-1"], {"role_permissions": {"r1": {"a": 4, "d": 0}}}),
    ]
    client = FakeClient()
    client.add_server(FakeServer(id="s1"))
    client.set_fetched_server(fresh)
    sender = _make_sender(client=client)

    await sender._place_via_server_edit(fresh, "ch-new", "Alpha")

    [payload] = fresh.server_edits
    assert payload["categories"][0]["role_permissions"] == {"r1": {"a": 4, "d": 0}}
    assert payload["categories"][0]["channels"] == ["ch-1", "ch-new"]


async def test_move_channel_to_category_top_rebuilds_from_a_freshly_fetched_server():
    stale = FakeServer(id="s1")
    stale.categories = [FakeCategory(id="cat-x", title="X", channels=["p1"])]
    fresh = FakeServer(id="s1")
    fresh.categories = [
        FakeCategory(id="cat-x", title="X", channels=["p1"]),
        FakeCategory(id="cat-y", title="Y", channels=["t1"]),
    ]
    client = FakeClient()
    client.add_server(stale)
    client.set_fetched_server(fresh)
    sender = _make_sender(client=client)

    await sender._move_channel_to_category_top(stale, "p1", "cat-y")

    [payload] = fresh.server_edits
    cats = {c["id"]: c["channels"] for c in payload["categories"]}
    assert cats == {"cat-x": [], "cat-y": ["p1", "t1"]}  # cat-y (post-startup) not lost


async def test_ensure_category_server_edit_fallback_builds_from_a_fresh_fetch():
    class OldStoatServer(FakeServer):
        async def create_category(self, name, *, channels):
            raise stoat.HTTPException.__new__(stoat.NotFound)

    fresh = OldStoatServer(id="s1")
    fresh.categories = [FakeCategory(id="cat-b", title="Beta", channels=["ch-2"])]
    client = FakeClient()
    client.add_server(OldStoatServer(id="s1"))  # stale cache: no categories
    client.set_fetched_server(fresh)
    sender = _make_sender(client=client)

    new_id = await sender.ensure_category("Gamma")

    [payload] = fresh.server_edits
    by_title = {c["title"]: c["id"] for c in payload["categories"]}
    assert by_title["Beta"] == "cat-b"  # the post-startup Category isn't dropped
    assert by_title["Gamma"] == new_id


async def test_ensure_category_reuses_an_existing_match_from_the_fresh_fetch():
    fresh = FakeServer(id="s1")
    fresh.categories = [FakeCategory(id="cat-real", title="Team", channels=["ch-1"])]
    client = FakeClient()
    client.add_server(FakeServer(id="s1"))  # stale cache doesn't know "Team"
    client.set_fetched_server(fresh)
    sender = _make_sender(client=client)

    assert await sender.ensure_category("team") == "cat-real"  # case-insensitive reuse
    assert fresh.server_edits == []  # nothing recreated


async def test_move_channel_to_category_builds_from_a_fresh_fetch():
    fresh = FakeServer(id="s1")
    fresh.categories = [
        FakeCategory(id="cat-x", title="X", channels=["a"]),
        FakeCategory(id="cat-y", title="Y", channels=[]),
    ]
    client = FakeClient()
    client.add_server(FakeServer(id="s1"))  # stale cache only knows cat-x
    client.set_fetched_server(fresh)
    sender = _make_sender(client=client)

    await sender.move_channel_to_category("a", "cat-y")

    [payload] = fresh.server_edits
    cats = {c["id"]: c["channels"] for c in payload["categories"]}
    assert cats == {"cat-x": [], "cat-y": ["a"]}


# ---- issue #66: the Category-list *readers* also re-fetch (short-TTL-cached),
# ---- since stoat.py never updates the cached `.categories` from gateway events,
# ---- so `/link category` for a Category created since startup would otherwise
# ---- fail to resolve the name and store the raw token as the "id".


class _CountingClient(FakeClient):
    def __init__(self) -> None:
        super().__init__()
        self.fetch_server_calls = 0

    async def fetch_server(self, server_id: str, *, populate_channels: bool = False):
        self.fetch_server_calls += 1
        return await super().fetch_server(server_id, populate_channels=populate_channels)


def _drifted_client() -> _CountingClient:
    """A cache that only knew "Alpha" at gateway-connect; the live server has
    gained "Counting" since."""
    stale = FakeServer(id="s1")
    stale.categories = [FakeCategory(id="cat-a", title="Alpha", channels=[])]
    fresh = FakeServer(id="s1")
    fresh.categories = [
        FakeCategory(id="cat-a", title="Alpha", channels=[]),
        FakeCategory(id="cat-count", title="Counting", channels=["ch-1"]),
    ]
    client = _CountingClient()
    client.add_server(stale)
    client.set_fetched_server(fresh)
    return client


async def test_resolve_category_id_by_name_finds_a_category_created_since_startup():
    sender = _make_sender(client=_drifted_client())

    assert await sender.resolve_category_id_by_name("counting") == "cat-count"
    assert await sender.resolve_category_id_by_name("cat-count") == "cat-count"
    assert await sender.resolve_category_id_by_name("nope") is None


async def test_get_category_name_finds_a_category_created_since_startup():
    sender = _make_sender(client=_drifted_client())

    assert await sender.get_category_name("cat-count") == "Counting"


async def test_list_categories_includes_a_category_created_since_startup():
    sender = _make_sender(client=_drifted_client())

    assert await sender.list_categories() == [("cat-a", "Alpha"), ("cat-count", "Counting")]


async def test_fresh_category_reads_are_ttl_cached():
    client = _drifted_client()
    sender = _make_sender(client=client)

    await sender.list_categories()
    await sender.resolve_category_id_by_name("counting")
    await sender.get_category_name("cat-count")

    assert client.fetch_server_calls == 1  # later reads served from the TTL cache


async def test_a_category_write_invalidates_the_read_cache():
    client = _drifted_client()
    sender = _make_sender(client=client)

    await sender.list_categories()
    fetches_before_write = client.fetch_server_calls

    await sender.ensure_category("Brand New")  # mutates the layout
    await sender.list_categories()

    # the post-write read re-fetched rather than serving the pre-write snapshot
    assert client.fetch_server_calls > fetches_before_write + 1


# --------------------------------- thread-Category binding (parent <-> category id)


class _BindingLinker:
    def __init__(self, bound: dict[str, str] | None = None) -> None:
        self.bound = dict(bound or {})  # parent_channel_id -> category_id
        self.binds: list[tuple[str, str]] = []
        self.forgotten: list[str] = []

    async def thread_category_id(self, connector_id, parent_channel_id):
        return self.bound.get(parent_channel_id)

    async def bind_thread_category(self, connector_id, parent_channel_id, category_id):
        self.bound[parent_channel_id] = category_id
        self.binds.append((parent_channel_id, category_id))

    async def forget_thread_category(self, connector_id, parent_channel_id):
        self.bound.pop(parent_channel_id, None)
        self.forgotten.append(parent_channel_id)

    async def is_thread_category(self, connector_id, category_id):
        return category_id in self.bound.values()


async def test_ensure_channel_binds_parent_to_category_on_first_thread():
    server = FakeServer(id="s1")
    server.categories.append(FakeCategory(id="cat-1", title="Bot Config", channels=[]))
    client = FakeClient()
    client.add_server(server)
    linker = _BindingLinker()
    sender = _make_sender(client=client, category_linker=linker)

    await sender.ensure_channel("general", "Bot Config", True, "p1")

    assert linker.binds == [("p1", "cat-1")]  # matched the existing Category by title, then bound it


async def test_ensure_channel_reuses_the_bound_category_by_id_after_a_rename():
    server = FakeServer(id="s1")
    server.categories.append(FakeCategory(id="cat-1", title="Renamed On Stoat", channels=["chan-other"]))
    client = FakeClient()
    client.add_server(server)
    linker = _BindingLinker({"p1": "cat-1"})
    sender = _make_sender(client=client, category_linker=linker)

    await sender.ensure_channel("general", "Bot Config", True, "p1")

    assert server.created_categories == []  # no new Category despite the title mismatch
    [category] = server.categories
    assert category.channels == ["chan-other", "chan-general"]


async def test_ensure_channel_self_heals_when_the_bound_category_is_gone():
    server = FakeServer(id="s1")  # nothing with id "cat-gone"
    client = FakeClient()
    client.add_server(server)
    linker = _BindingLinker({"p1": "cat-gone"})
    sender = _make_sender(client=client, category_linker=linker)

    await sender.ensure_channel("general", "Bot Config", True, "p1")

    assert linker.forgotten == ["p1"]  # stale binding dropped
    [category] = server.categories
    assert category.title == "Bot Config"  # fresh Category created by the linked parent name
    assert linker.bound["p1"] == category.id  # and rebound to the new id
    assert linker.binds == [("p1", category.id)]


async def test_ensure_channel_keeps_a_bound_thread_category_absent_from_the_stale_cache():
    # The thread Category was created on an earlier thread via the raw-HTTP
    # PATCH, which doesn't refresh the client cache - so `get_server` still
    # doesn't list it. Judging "is it gone?" off the cache would forget the
    # binding and spawn a duplicate Category for every later thread (issue #27,
    # thread path). The bound-category check must run against a fresh fetch.
    stale = FakeServer(id="s1")  # cache never saw cat-thread
    fresh = FakeServer(id="s1")
    fresh.categories = [FakeCategory(id="cat-thread", title="Renamed On Stoat", channels=["chan-other"])]
    client = FakeClient()
    client.add_server(stale)
    client.set_fetched_server(fresh)
    linker = _BindingLinker({"p1": "cat-thread"})
    sender = _make_sender(client=client, category_linker=linker)

    await sender.ensure_channel("general", "Bot Config", True, "p1")

    assert linker.forgotten == []  # binding kept - the Category isn't actually gone
    assert linker.binds == [("p1", "cat-thread")]  # re-affirmed to the same id, not a new one
    assert fresh.created_categories == []  # no duplicate Category
    [category] = fresh.categories
    assert category.channels == ["chan-other", "chan-general"]  # channel added to the bound one


async def test_ensure_channel_dedupes_against_a_freshly_fetched_channel_list():
    # A channel created since gateway-connect (e.g. by another connector's
    # mirror) that the cache doesn't list must not be re-created.
    stale = FakeServer(id="s1")
    fresh = FakeServer(id="s1")
    fresh.channels = [FakeChannel(id="chan-existing", name="general")]
    client = FakeClient()
    client.add_server(stale)
    client.set_fetched_server(fresh)
    sender = _make_sender(client=client)

    channel_id = await sender.ensure_channel("general")

    assert channel_id == "chan-existing"
    assert fresh.created_channels == []


async def test_ensure_channel_groups_the_parent_channel_atop_the_thread_category():
    # `/mirror channel` on a Discord thread must pull the parent channel up into
    # the freshly-created thread Category now, not leave it to the next relayed
    # message (issue #94) - `group_parent_channel_with_threads` on the relay path
    # reads the cache-only Category list, which never carries this brand-new
    # Category.
    server = FakeServer(id="s1")
    server.channels = [FakeChannel(id="p1", name="bot-config"), FakeChannel(id="chan-general", name="general")]
    server.categories = [
        FakeCategory(id="cat-admin", title="Admin", channels=["p1"]),
        FakeCategory(id="cat-1", title="Bot Config", channels=[]),
    ]
    client = FakeClient()
    client.add_server(server)
    linker = _BindingLinker()
    sender = _make_sender(client=client, category_linker=linker)

    await sender.ensure_channel("general", "Bot Config", True, "p1")

    assert linker.binds == [("p1", "cat-1")]
    [payload] = server.server_edits
    cats = {c["title"]: c["channels"] for c in payload["categories"]}
    assert cats["Admin"] == []  # parent pulled out of its old category
    assert cats["Bot Config"] == ["p1", "chan-general"]  # parent first, then the thread channel


async def test_ensure_channel_skips_the_parent_group_when_it_already_leads_the_category():
    server = FakeServer(id="s1")
    server.channels = [FakeChannel(id="p1", name="bot-config"), FakeChannel(id="chan-general", name="general")]
    server.categories = [FakeCategory(id="cat-1", title="Bot Config", channels=["p1"])]
    client = FakeClient()
    client.add_server(server)
    sender = _make_sender(client=client, category_linker=_BindingLinker())

    await sender.ensure_channel("general", "Bot Config", True, "p1")

    assert server.server_edits == []  # parent already on top - nothing rebuilt


async def test_ensure_channel_skips_the_parent_group_when_the_option_is_off():
    server = FakeServer(id="s1")
    server.channels = [FakeChannel(id="p1", name="bot-config"), FakeChannel(id="chan-general", name="general")]
    server.categories = [
        FakeCategory(id="cat-admin", title="Admin", channels=["p1"]),
        FakeCategory(id="cat-1", title="Bot Config", channels=[]),
    ]
    client = FakeClient()
    client.add_server(server)
    sender = _make_sender(client=client, category_linker=_BindingLinker())
    sender._config = SimpleNamespace(group_parent_channel_with_threads=False)

    await sender.ensure_channel("general", "Bot Config", True, "p1")

    assert server.server_edits == []


# ---------------------------------------------------------------- _linked_channels


async def test_linked_channels_reports_the_invoking_channel():
    linker = FakeLinker()
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


# ---------------------------------------------------------------- role commands


async def test_link_role_success():
    role_linker = FakeRoleLinker()
    sender = _make_sender(role_linker=role_linker)
    ctx = _make_ctx()

    await sender._link_role(ctx, "Mods", "discord", "111")

    assert role_linker.link_role_calls == [
        {"local_connector": "stoat", "local_role": "Mods", "source": "discord", "source_role": "111"}
    ]
    assert ctx.channel.sent[0]["content"] == "role linked ok"


async def test_link_role_without_a_configured_role_linker():
    sender = _make_sender(role_linker=None)
    ctx = _make_ctx()

    await sender._link_role(ctx, "Mods", "discord", "111")

    assert ctx.channel.sent[0]["content"] == "Role linking isn't configured."


async def test_mirror_and_linked_and_unlink_role_route():
    role_linker = FakeRoleLinker()
    sender = _make_sender(role_linker=role_linker)
    ctx = _make_ctx()

    await sender._mirror_role(ctx, "Mods")
    await sender._mirror_role(ctx, "Mods", "stoat")
    await sender._linked_roles(ctx)
    await sender._unlink_role(ctx, "Mods", "all")

    assert role_linker.mirror_role_all_calls == [{"local_connector": "stoat", "local_role": "Mods"}]
    assert role_linker.mirror_role_calls == [
        {"local_connector": "stoat", "local_role": "Mods", "destination": "stoat", "new_name": None}
    ]
    assert role_linker.list_linked_roles_calls == [
        {"local_connector": "stoat", "local_role": None, "service": None}
    ]
    assert role_linker.unlink_role_calls == [
        {"local_connector": "stoat", "local_role": "Mods", "destination": "all"}
    ]


async def test_linked_roles_needs_no_admin_permission():
    role_linker = FakeRoleLinker()
    sender = _make_sender(role_linker=role_linker)
    ctx = _make_ctx(manage_server=False)

    await sender._linked_roles(ctx, "Mods")

    assert role_linker.list_linked_roles_calls == [
        {"local_connector": "stoat", "local_role": "Mods", "service": None}
    ]


# ---------------------------------------------------------------- _handle_channel_create


async def test_handle_channel_create_syncs_a_new_channel_in_a_linked_category():
    category_linker = FakeCategoryLinker()
    sender = _make_sender(category_linker=category_linker, server_id="s1")
    category = FakeCategory(id="cat-1", title="Team")
    channel = FakeChannel(id="c2", name="general-2", server_id="s1", category=category)

    await sender._handle_channel_create(channel)

    assert category_linker.sync_new_channel_calls == [
        {
            "local_connector": "stoat",
            "local_category_id": "cat-1",
            "channel_id": "c2",
            "channel_name": "general-2",
        }
    ]


async def test_handle_channel_create_noop_without_a_configured_category_linker():
    sender = _make_sender(category_linker=None, server_id="s1")
    category = FakeCategory(id="cat-1", title="Team")
    channel = FakeChannel(id="c2", name="general-2", server_id="s1", category=category)

    await sender._handle_channel_create(channel)  # would raise if it tried to use a None category_linker


async def test_handle_channel_create_noop_for_a_channel_on_a_different_server():
    category_linker = FakeCategoryLinker()
    sender = _make_sender(category_linker=category_linker, server_id="s1")
    category = FakeCategory(id="cat-1", title="Team")
    channel = FakeChannel(id="c2", name="general-2", server_id="other-server", category=category)

    await sender._handle_channel_create(channel)

    assert category_linker.sync_new_channel_calls == []


async def test_handle_channel_create_noop_for_a_channel_with_no_category():
    category_linker = FakeCategoryLinker()
    sender = _make_sender(category_linker=category_linker, server_id="s1")
    channel = FakeChannel(id="c2", name="general-2", server_id="s1", category=None)

    await sender._handle_channel_create(channel)

    assert category_linker.sync_new_channel_calls == []
