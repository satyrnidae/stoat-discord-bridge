"""Shared fixtures/fakes for the StoatSenderService admin-command dispatch
suite (`tests/stoat_admin/`) - the `_link_*` / `_mirror_channel` / `_linked_*`
/ `_unlink_*` methods the `stoat.ext.commands` tree on `_StoatClient` forwards
to, and the `_is_admin` Manage-Server gate behind the mutating ones.

Constructs the service via object.__new__, same rationale as
test_stoat_resolve_avatar.py/test_stoat_sender_dispatch.py: __init__ builds
a _StoatClient whose constructor makes a real network call that none of
these handlers need.
"""

from __future__ import annotations

from collections import deque
from types import SimpleNamespace

from stoat_discord_bridge.admin_commands import LinkError
from stoat_discord_bridge.services.stoat_service import StoatSenderService
from tests.fakes.fake_stoat import FakeChannel, FakeClient
from tests.fakes.fake_linkers import FakeLinker, FakeUserLinker

__all__ = [
    "FakeLinker",
    "FakeUserLinker",
    "FakeEmoteLinker",
    "FakeCategoryLinker",
    "FakeRoleLinker",
    "_make_sender",
    "_admin_message",
    "_Ctx",
    "_make_ctx",
]


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
