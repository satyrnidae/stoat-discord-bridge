"""Tests for StoatSenderService's admin-command surface -
_handle_mirror_channels/_handle_link_channel/_handle_link_emote/
_handle_link_user/_handle_mirror_channel and the _is_admin permission gate
behind all of them. Previously almost entirely untested - this was
stoat_service.py's single largest coverage gap.

Constructs the service via object.__new__, same rationale as
test_stoat_resolve_avatar.py/test_stoat_sender_dispatch.py: __init__ builds
a _StoatClient whose constructor makes a real network call that none of
these handlers need.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
import stoat

from stoat_discord_bridge.admin_commands import LinkError
from stoat_discord_bridge.channel_structure import ChannelSpec, GroupSpec, GuildStructure
from stoat_discord_bridge.services.stoat_service import StoatSenderService
from tests.fakes.fake_stoat import FakeCategory, FakeChannel, FakeClient, FakeServer


class FakeLinker:
    def __init__(self, *, raises: LinkError | None = None) -> None:
        self._raises = raises
        self.link_channel_calls: list[dict] = []
        self.mirror_channel_calls: list[dict] = []
        self.mirror_channel_all_calls: list[dict] = []
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

    async def unlink_channel(self, **kwargs):
        self.unlink_channel_calls.append(kwargs)
        if self._raises is not None:
            raise self._raises
        return "unlinked ok"


class FakeEmoteLinker:
    def __init__(self, *, raises: LinkError | None = None) -> None:
        self._raises = raises
        self.calls: list[dict] = []

    async def link_emote(self, **kwargs):
        self.calls.append(kwargs)
        if self._raises is not None:
            raise self._raises
        return "emote linked ok"


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


class FakeMirrorer:
    def __init__(self, *, structure: GuildStructure | None = None, raises: Exception | None = None) -> None:
        self._structure = structure
        self._raises = raises
        self.get_structure_calls: list[str] = []

    def get_structure(self, source: str) -> GuildStructure:
        self.get_structure_calls.append(source)
        if self._raises is not None:
            raise self._raises
        return self._structure


class FakeRoleLinker:
    def __init__(self, *, raises: LinkError | None = None) -> None:
        self._raises = raises
        self.link_role_calls: list[dict] = []
        self.unlink_role_calls: list[dict] = []
        self.list_linked_roles_calls: list[dict] = []
        self.mirror_role_calls: list[dict] = []
        self.mirror_role_all_calls: list[dict] = []

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


def _make_sender(
    *,
    linker: FakeLinker | None = None,
    mirrorer: FakeMirrorer | None = None,
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
    sender._mirrorer = mirrorer
    sender._emote_linker = emote_linker
    sender._user_linker = user_linker
    sender._category_linker = category_linker
    sender._role_linker = role_linker
    sender.server_id = server_id
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


# ---------------------------------------------------------------- shared "needs admin" gate


async def test_each_admin_command_rejects_a_non_admin():
    sender = _make_sender(
        linker=FakeLinker(),
        emote_linker=FakeEmoteLinker(),
        user_linker=FakeUserLinker(),
        mirrorer=FakeMirrorer(),
        category_linker=FakeCategoryLinker(),
    )
    message = _admin_message(manage_server=False)

    await sender._handle_mirror_channels(message, ["discord"])
    await sender._handle_link_channel(message, ["discord", "s1"])
    await sender._handle_link_emote(message, ["discord", "s1", "l1"])
    await sender._handle_link_user(message, ["discord", "u1", "l1"])
    await sender._handle_mirror_channel(message, ["discord"])
    await sender._handle_unlink_channel(message, [])
    await sender._handle_unlink_user(message, [])
    await sender._handle_link_category(message, ["discord", "s1"])
    await sender._handle_unlink_category(message, [])

    assert message.channel.sent == [{"content": "You need the Manage Server permission to do that.", "masquerade": None}] * 9


# ---------------------------------------------------------------- _handle_link_channel


async def test_link_channel_success():
    linker = FakeLinker()
    sender = _make_sender(linker=linker)
    message = _admin_message(channel=FakeChannel(id="c1", name="general"))

    await sender._handle_link_channel(message, ["dest-id", "discord", "src-id"])

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
    assert message.channel.sent[0]["content"] == "linked ok"


async def test_link_channel_destination_defaults_to_none():
    linker = FakeLinker()
    sender = _make_sender(linker=linker)
    message = _admin_message()

    await sender._handle_link_channel(message, ["discord", "src-id"])

    assert linker.link_channel_calls[0]["destination_id"] is None


async def test_link_channel_wrong_arg_count_sends_usage():
    sender = _make_sender(linker=FakeLinker())
    message = _admin_message()

    await sender._handle_link_channel(message, ["discord"])

    assert (
        message.channel.sent[0]["content"]
        == "Usage: /link channel [local_id|name] <service> <external_id|name>"
    )


async def test_link_channel_without_a_configured_linker():
    sender = _make_sender(linker=None)
    message = _admin_message()

    await sender._handle_link_channel(message, ["discord", "src-id"])

    assert message.channel.sent[0]["content"] == "Linking isn't configured."


async def test_link_channel_reports_a_link_error():
    sender = _make_sender(linker=FakeLinker(raises=LinkError("already linked elsewhere")))
    message = _admin_message()

    await sender._handle_link_channel(message, ["discord", "src-id"])

    assert message.channel.sent[0]["content"] == "already linked elsewhere"


# ---------------------------------------------------------------- _handle_link_emote


async def test_link_emote_success():
    emote_linker = FakeEmoteLinker()
    sender = _make_sender(emote_linker=emote_linker)
    message = _admin_message()

    await sender._handle_link_emote(message, ["discord", "src-id", "local-id"])

    assert emote_linker.calls == [{"local_connector": "stoat", "local_id": "local-id", "source": "discord", "source_id": "src-id"}]
    assert message.channel.sent[0]["content"] == "emote linked ok"


async def test_link_emote_wrong_arg_count_sends_usage():
    sender = _make_sender(emote_linker=FakeEmoteLinker())
    message = _admin_message()

    await sender._handle_link_emote(message, ["discord", "src-id"])

    assert message.channel.sent[0]["content"] == "Usage: /link-emote <service> <external_id> <local_id>"


async def test_link_emote_without_a_configured_linker():
    sender = _make_sender(emote_linker=None)
    message = _admin_message()

    await sender._handle_link_emote(message, ["discord", "src-id", "local-id"])

    assert message.channel.sent[0]["content"] == "Linking isn't configured."


# ---------------------------------------------------------------- _handle_link_user


async def test_link_user_success():
    user_linker = FakeUserLinker()
    sender = _make_sender(user_linker=user_linker)
    message = _admin_message()

    await sender._handle_link_user(message, ["discord", "remote-id", "local-id"])

    assert user_linker.calls == [
        {"local_connector": "stoat", "local_user_id": "local-id", "source": "discord", "source_user_id": "remote-id"}
    ]
    assert message.channel.sent[0]["content"] == "user linked ok"


async def test_link_user_wrong_arg_count_sends_usage():
    sender = _make_sender(user_linker=FakeUserLinker())
    message = _admin_message()

    await sender._handle_link_user(message, ["discord", "remote-id"])

    assert message.channel.sent[0]["content"] == "Usage: /link user <service> <external_id|name> <local_id|name>"


async def test_link_user_without_a_configured_linker():
    sender = _make_sender(user_linker=None)
    message = _admin_message()

    await sender._handle_link_user(message, ["discord", "remote-id", "local-id"])

    assert message.channel.sent[0]["content"] == "User linking isn't configured."


# ---------------------------------------------------------------- _handle_mirror_channel


async def test_mirror_channel_to_a_single_destination():
    linker = FakeLinker()
    sender = _make_sender(linker=linker)
    message = _admin_message(channel=FakeChannel(id="c1", name="general"))

    await sender._handle_mirror_channel(message, ["general", "discord"])

    assert linker.mirror_channel_calls == [
        {
            "local_connector": "stoat",
            "local_channel_id": "general",
            "local_channel_name": "general",
            "destination": "discord",
            "local_channel_category": None,
        }
    ]
    assert message.channel.sent[0]["content"] == "mirrored ok"


async def test_mirror_channel_resolves_and_forwards_the_channels_category():
    linker = FakeLinker()
    channel = FakeChannel(id="c1", name="general", category=FakeCategory(id="cat-1", title="Team Alpha"))
    client = FakeClient()
    client.add_channel(channel)
    sender = _make_sender(linker=linker, client=client)
    message = _admin_message(channel=channel)

    await sender._handle_mirror_channel(message, ["c1", "discord"])

    assert linker.mirror_channel_calls[0]["local_channel_category"] == "Team Alpha"


async def test_mirror_channel_to_all_is_case_insensitive():
    linker = FakeLinker()
    sender = _make_sender(linker=linker)
    message = _admin_message()

    await sender._handle_mirror_channel(message, ["general", "ALL"])

    assert linker.mirror_channel_all_calls
    assert message.channel.sent[0]["content"] == "mirrored to all ok"


async def test_mirror_channel_no_args_mirrors_the_current_channel_to_all():
    linker = FakeLinker()
    sender = _make_sender(linker=linker)
    message = _admin_message(channel=FakeChannel(id="c1", name="general"))

    await sender._handle_mirror_channel(message, [])

    assert linker.mirror_channel_all_calls[0]["local_channel_id"] == "c1"


async def test_mirror_channel_uses_an_explicit_channel_id_when_given():
    linker = FakeLinker()
    sender = _make_sender(linker=linker)
    message = _admin_message()

    await sender._handle_mirror_channel(message, ["explicit-id", "discord"])

    call = linker.mirror_channel_calls[0]
    assert call["local_channel_id"] == "explicit-id"
    assert call["local_channel_name"] == "explicit-id"


async def test_mirror_channel_without_a_configured_linker():
    sender = _make_sender(linker=None)
    message = _admin_message()

    await sender._handle_mirror_channel(message, ["discord"])

    assert message.channel.sent[0]["content"] == "Linking isn't configured."


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


# ---------------------------------------------------------------- _handle_mirror_channels


async def test_mirror_channels_success_creates_and_links():
    structure = GuildStructure(
        groups=[GroupSpec(name="Text", channels=[ChannelSpec(name="general", source_channel_id="d1")])],
        ungrouped_channels=[],
    )
    mirrorer = FakeMirrorer(structure=structure)
    linker = FakeLinker()
    sender = _make_sender(mirrorer=mirrorer, linker=linker)
    server = FakeServer(id="s1")
    channel = FakeChannel(id="c1")
    channel.server = server
    message = _admin_message(channel=channel)

    await sender._handle_mirror_channels(message, ["discord"])

    assert mirrorer.get_structure_calls == ["discord"]
    assert server.created_channels == ["general"]
    assert linker.link_channel_calls
    assert "Mirrored 'discord' structure" in channel.sent[-1]["content"]


async def test_mirror_channels_missing_source_sends_usage():
    sender = _make_sender(mirrorer=FakeMirrorer())
    message = _admin_message()

    await sender._handle_mirror_channels(message, [])

    assert message.channel.sent[0]["content"] == "Usage: /mirror-channels <service>"


async def test_mirror_channels_without_a_configured_mirrorer():
    sender = _make_sender(mirrorer=None)
    message = _admin_message()

    await sender._handle_mirror_channels(message, ["discord"])

    assert message.channel.sent[0]["content"] == "Mirroring isn't configured."


async def test_mirror_channels_reports_a_link_error_from_get_structure():
    mirrorer = FakeMirrorer(raises=LinkError("'discord' isn't a known structure source"))
    sender = _make_sender(mirrorer=mirrorer)
    message = _admin_message()

    await sender._handle_mirror_channels(message, ["discord"])

    assert message.channel.sent[0]["content"] == "'discord' isn't a known structure source"


async def test_mirror_channels_reports_an_unexpected_error_from_get_structure():
    mirrorer = FakeMirrorer(raises=RuntimeError("boom"))
    sender = _make_sender(mirrorer=mirrorer)
    message = _admin_message()

    await sender._handle_mirror_channels(message, ["discord"])

    assert "Couldn't read the 'discord' channel structure: boom" in message.channel.sent[0]["content"]


# ---------------------------------------------------------------- _handle_linked_channels


async def test_linked_channels_reports_the_invoking_channel():
    linker = FakeLinker()
    sender = _make_sender(linker=linker)
    channel = FakeChannel(id="c1")
    message = _admin_message(channel=channel)

    await sender._handle_linked_channels(message)

    assert linker.list_linked_channels_calls == [{"local_connector": "stoat", "local_channel_id": "c1"}]
    assert channel.sent[0]["content"] == "Linked channels:\nStoat: general (c1) (this channel)"


async def test_linked_channels_without_a_configured_linker():
    sender = _make_sender(linker=None)
    message = _admin_message()

    await sender._handle_linked_channels(message)

    assert message.channel.sent[0]["content"] == "Linking isn't configured."


async def test_linked_channels_needs_no_admin_permission():
    linker = FakeLinker()
    sender = _make_sender(linker=linker)
    message = _admin_message(manage_server=False)

    await sender._handle_linked_channels(message)  # must not be rejected

    assert linker.list_linked_channels_calls


# ---------------------------------------------------------------- _handle_linked_users


async def test_linked_users_with_an_argument_shows_only_that_users_link():
    user_linker = FakeUserLinker()
    sender = _make_sender(user_linker=user_linker)
    message = _admin_message()

    await sender._handle_linked_users(message, ["01KH7TH31EBY08FTQ7YC2RC4DQ"])

    assert user_linker.list_linked_users_calls == [
        {"local_connector": "stoat", "local_user_id": "01KH7TH31EBY08FTQ7YC2RC4DQ"}
    ]
    assert message.channel.sent[0]["content"] == "Linked users:\nDiscord: ShrinerH (216591124222050304) ↔ Stoat: shriner (01KH)"


async def test_linked_users_with_no_argument_lists_everything():
    user_linker = FakeUserLinker()
    sender = _make_sender(user_linker=user_linker)
    message = _admin_message()

    await sender._handle_linked_users(message, [])

    assert user_linker.list_linked_users_calls == [{}]


async def test_linked_users_without_a_configured_user_linker():
    sender = _make_sender(user_linker=None)
    message = _admin_message()

    await sender._handle_linked_users(message, [])

    assert message.channel.sent[0]["content"] == "User linking isn't configured."


async def test_linked_users_needs_no_admin_permission():
    user_linker = FakeUserLinker()
    sender = _make_sender(user_linker=user_linker)
    message = _admin_message(manage_server=False)

    await sender._handle_linked_users(message, [])  # must not be rejected

    assert user_linker.list_linked_users_calls


# ---------------------------------------------------------------- _handle_unlink_channel


async def test_unlink_channel_defaults_to_all():
    linker = FakeLinker()
    sender = _make_sender(linker=linker)
    message = _admin_message(channel=FakeChannel(id="c1"))

    await sender._handle_unlink_channel(message, [])

    assert linker.unlink_channel_calls == [{"local_connector": "stoat", "local_channel_id": "c1", "destination": None}]
    assert message.channel.sent[0]["content"] == "unlinked ok"


async def test_unlink_channel_with_a_specific_destination():
    linker = FakeLinker()
    sender = _make_sender(linker=linker)
    message = _admin_message(channel=FakeChannel(id="c1"))

    await sender._handle_unlink_channel(message, ["c1", "discord"])

    assert linker.unlink_channel_calls == [{"local_connector": "stoat", "local_channel_id": "c1", "destination": "discord"}]


async def test_unlink_channel_with_a_specific_local_channel_id():
    linker = FakeLinker()
    sender = _make_sender(linker=linker)
    message = _admin_message(channel=FakeChannel(id="c1"))

    await sender._handle_unlink_channel(message, ["other-channel", "discord"])

    assert linker.unlink_channel_calls == [
        {"local_connector": "stoat", "local_channel_id": "other-channel", "destination": "discord"}
    ]


async def test_unlink_channel_without_a_configured_linker():
    sender = _make_sender(linker=None)
    message = _admin_message()

    await sender._handle_unlink_channel(message, [])

    assert message.channel.sent[0]["content"] == "Linking isn't configured."


async def test_unlink_channel_reports_a_link_error():
    linker = FakeLinker(raises=LinkError("this channel isn't linked to anything."))
    sender = _make_sender(linker=linker)
    message = _admin_message()

    await sender._handle_unlink_channel(message, [])

    assert message.channel.sent[0]["content"] == "this channel isn't linked to anything."


# ---------------------------------------------------------------- _handle_unlink_user


async def test_unlink_user_defaults_to_all_and_self():
    user_linker = FakeUserLinker()
    sender = _make_sender(user_linker=user_linker)
    message = _admin_message()

    await sender._handle_unlink_user(message, [])

    assert user_linker.unlink_user_calls == [{"local_connector": "stoat", "local_user_id": "admin-1", "destination": None}]
    assert message.channel.sent[0]["content"] == "user unlinked ok"


async def test_unlink_user_with_a_specific_destination_and_target():
    user_linker = FakeUserLinker()
    sender = _make_sender(user_linker=user_linker)
    message = _admin_message()

    await sender._handle_unlink_user(message, ["discord", "s1"])

    assert user_linker.unlink_user_calls == [{"local_connector": "stoat", "local_user_id": "s1", "destination": "discord"}]


async def test_unlink_user_without_a_configured_user_linker():
    sender = _make_sender(user_linker=None)
    message = _admin_message()

    await sender._handle_unlink_user(message, [])

    assert message.channel.sent[0]["content"] == "User linking isn't configured."


async def test_unlink_user_reports_a_link_error():
    user_linker = FakeUserLinker(raises=LinkError("this user isn't linked to anything."))
    sender = _make_sender(user_linker=user_linker)
    message = _admin_message()

    await sender._handle_unlink_user(message, [])

    assert message.channel.sent[0]["content"] == "this user isn't linked to anything."


# ---------------------------------------------------------------- _handle_linked_categories


async def test_linked_categories_reports_the_invoking_channels_category():
    category_linker = FakeCategoryLinker()
    channel = FakeChannel(id="c1", category=FakeCategory(id="cat-1", title="Team"))
    sender = _make_sender(category_linker=category_linker)
    message = _admin_message(channel=channel)

    await sender._handle_linked_categories(message, [])

    assert category_linker.list_linked_categories_calls == [
        {"local_connector": "stoat", "local_category_id": "cat-1", "local_category": None}
    ]
    assert channel.sent[0]["content"] == "Linked categories:\nStoat: Team (cat-1) (this Category)"


async def test_linked_categories_without_a_configured_category_linker():
    sender = _make_sender(category_linker=None)
    message = _admin_message()

    await sender._handle_linked_categories(message, [])

    assert message.channel.sent[0]["content"] == "Category linking isn't configured."


async def test_linked_categories_when_invoking_channel_has_no_category():
    category_linker = FakeCategoryLinker()
    channel = FakeChannel(id="c1", category=None)
    sender = _make_sender(category_linker=category_linker)
    message = _admin_message(channel=channel)

    await sender._handle_linked_categories(message, [])

    assert message.channel.sent[0]["content"] == "This channel isn't in a Category."
    assert category_linker.list_linked_categories_calls == []


async def test_linked_categories_needs_no_admin_permission():
    category_linker = FakeCategoryLinker()
    channel = FakeChannel(id="c1", category=FakeCategory(id="cat-1", title="Team"))
    sender = _make_sender(category_linker=category_linker)
    message = _admin_message(manage_server=False, channel=channel)

    await sender._handle_linked_categories(message, [])  # must not be rejected

    assert category_linker.list_linked_categories_calls


# ---------------------------------------------------------------- _handle_link_category


async def test_link_category_success():
    category_linker = FakeCategoryLinker()
    channel = FakeChannel(id="c1", category=FakeCategory(id="cat-1", title="Team"))
    sender = _make_sender(category_linker=category_linker)
    message = _admin_message(channel=channel)

    await sender._handle_link_category(message, ["discord", "src-id", "dest-id"])

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
    message = _admin_message(channel=channel)

    await sender._handle_link_category(message, ["discord", "src-id"])

    assert category_linker.link_category_calls[0]["destination_id"] is None


async def test_link_category_wrong_arg_count_sends_usage():
    sender = _make_sender(category_linker=FakeCategoryLinker())
    message = _admin_message()

    await sender._handle_link_category(message, ["discord"])

    assert (
        message.channel.sent[0]["content"]
        == "Usage: /link category <service> <external_id|name> [<local_id|name>]"
    )


async def test_link_category_without_a_configured_category_linker():
    sender = _make_sender(category_linker=None)
    message = _admin_message()

    await sender._handle_link_category(message, ["discord", "src-id"])

    assert message.channel.sent[0]["content"] == "Category linking isn't configured."


async def test_link_category_when_invoking_channel_has_no_category():
    category_linker = FakeCategoryLinker()
    channel = FakeChannel(id="c1", category=None)
    sender = _make_sender(category_linker=category_linker)
    message = _admin_message(channel=channel)

    await sender._handle_link_category(message, ["discord", "src-id"])

    assert message.channel.sent[0]["content"] == "This channel isn't in a Category."
    assert category_linker.link_category_calls == []


async def test_link_category_reports_a_link_error():
    channel = FakeChannel(id="c1", category=FakeCategory(id="cat-1", title="Team"))
    sender = _make_sender(category_linker=FakeCategoryLinker(raises=LinkError("that Category is used for thread mirroring")))
    message = _admin_message(channel=channel)

    await sender._handle_link_category(message, ["discord", "src-id"])

    assert channel.sent[0]["content"] == "that Category is used for thread mirroring"


# ---------------------------------------------------------------- _handle_unlink_category


async def test_unlink_category_defaults_to_all():
    category_linker = FakeCategoryLinker()
    channel = FakeChannel(id="c1", category=FakeCategory(id="cat-1", title="Team"))
    sender = _make_sender(category_linker=category_linker)
    message = _admin_message(channel=channel)

    await sender._handle_unlink_category(message, [])

    assert category_linker.unlink_category_calls == [
        {"local_connector": "stoat", "local_category_id": "cat-1", "local_category": None, "destination": None}
    ]
    assert channel.sent[0]["content"] == "category unlinked ok"


async def test_unlink_category_with_a_specific_destination():
    category_linker = FakeCategoryLinker()
    channel = FakeChannel(id="c1", category=FakeCategory(id="cat-1", title="Team"))
    sender = _make_sender(category_linker=category_linker)
    message = _admin_message(channel=channel)

    await sender._handle_unlink_category(message, ["Team", "discord"])

    assert category_linker.unlink_category_calls == [
        {"local_connector": "stoat", "local_category_id": "cat-1", "local_category": "Team", "destination": "discord"}
    ]


async def test_unlink_category_without_a_configured_category_linker():
    sender = _make_sender(category_linker=None)
    message = _admin_message()

    await sender._handle_unlink_category(message, [])

    assert message.channel.sent[0]["content"] == "Category linking isn't configured."


async def test_unlink_category_when_invoking_channel_has_no_category():
    category_linker = FakeCategoryLinker()
    channel = FakeChannel(id="c1", category=None)
    sender = _make_sender(category_linker=category_linker)
    message = _admin_message(channel=channel)

    await sender._handle_unlink_category(message, [])

    assert message.channel.sent[0]["content"] == "This channel isn't in a Category."
    assert category_linker.unlink_category_calls == []


async def test_unlink_category_reports_a_link_error():
    channel = FakeChannel(id="c1", category=FakeCategory(id="cat-1", title="Team"))
    sender = _make_sender(category_linker=FakeCategoryLinker(raises=LinkError("this Category isn't linked to anything.")))
    message = _admin_message(channel=channel)

    await sender._handle_unlink_category(message, [])

    assert channel.sent[0]["content"] == "this Category isn't linked to anything."


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


# ---------------------------------------------------------------- /link channel & /link role two-token routing


def _cmd_message(content: str, *, manage_server: bool = True):
    msg = _admin_message(manage_server=manage_server)
    msg.content = content
    msg.id = "m1"
    return msg


async def test_two_token_link_role_routes():
    role_linker = FakeRoleLinker()
    sender = _make_sender(role_linker=role_linker)
    await sender._handle_message(_cmd_message("/link role Mods discord 111"))
    assert role_linker.link_role_calls == [
        {"local_connector": "stoat", "local_role": "Mods", "source": "discord", "source_role": "111"}
    ]


async def test_two_token_mirror_and_linked_and_unlink_route():
    role_linker = FakeRoleLinker()
    sender = _make_sender(role_linker=role_linker)
    await sender._handle_message(_cmd_message("/mirror role Mods"))
    await sender._handle_message(_cmd_message("/mirror role Mods stoat"))
    await sender._handle_message(_cmd_message("/linked roles"))
    await sender._handle_message(_cmd_message("/unlink role Mods all"))
    assert role_linker.mirror_role_all_calls == [{"local_connector": "stoat", "local_role": "Mods"}]
    assert role_linker.mirror_role_calls == [
        {"local_connector": "stoat", "local_role": "Mods", "destination": "stoat"}
    ]
    assert role_linker.list_linked_roles_calls == [
        {"local_connector": "stoat", "local_role": None, "service": None}
    ]
    assert role_linker.unlink_role_calls == [
        {"local_connector": "stoat", "local_role": "Mods", "destination": "all"}
    ]


async def test_link_role_non_admin_rejected_linked_roles_allowed():
    role_linker = FakeRoleLinker()
    sender = _make_sender(role_linker=role_linker)
    await sender._handle_message(_cmd_message("/link role Mods discord 111", manage_server=False))
    await sender._handle_message(_cmd_message("/linked roles Mods", manage_server=False))
    assert role_linker.link_role_calls == []
    assert role_linker.list_linked_roles_calls == [
        {"local_connector": "stoat", "local_role": "Mods", "service": None}
    ]


async def test_two_token_channel_commands_route_to_their_handlers():
    linker = FakeLinker()
    sender = _make_sender(linker=linker, role_linker=FakeRoleLinker())
    await sender._handle_message(_cmd_message("/link channel dest discord src"))
    await sender._handle_message(_cmd_message("/mirror channel general discord"))
    await sender._handle_message(_cmd_message("/unlink channel general discord"))
    await sender._handle_message(_cmd_message("/linked channels general"))
    assert linker.link_channel_calls == [
        {
            "local_connector": "stoat",
            "local_channel_id": "c1",
            "local_channel_name": "general",
            "source": "discord",
            "source_id": "src",
            "destination_id": "dest",
        }
    ]
    assert linker.mirror_channel_calls[0]["destination"] == "discord"
    assert linker.unlink_channel_calls == [
        {"local_connector": "stoat", "local_channel_id": "general", "destination": "discord"}
    ]
    assert linker.list_linked_channels_calls == [
        {"local_connector": "stoat", "local_channel_id": "general"}
    ]


async def test_two_token_channel_and_role_commands_do_not_shadow_each_other():
    linker = FakeLinker()
    role_linker = FakeRoleLinker()
    sender = _make_sender(linker=linker, role_linker=role_linker)
    await sender._handle_message(_cmd_message("/link channel dest discord src"))
    await sender._handle_message(_cmd_message("/link role Mods discord 111"))
    assert len(linker.link_channel_calls) == 1
    assert len(role_linker.link_role_calls) == 1


async def test_two_token_link_user_routes():
    user_linker = FakeUserLinker()
    sender = _make_sender(user_linker=user_linker)
    await sender._handle_message(_cmd_message("/link user discord Alice Bob"))
    assert user_linker.calls == [
        {"local_connector": "stoat", "local_user_id": "Bob", "source": "discord", "source_user_id": "Alice"}
    ]


async def test_two_token_unlink_and_linked_user_route():
    user_linker = FakeUserLinker()
    sender = _make_sender(user_linker=user_linker)
    await sender._handle_message(_cmd_message("/unlink user discord Bob"))
    await sender._handle_message(_cmd_message("/linked users"))
    assert user_linker.unlink_user_calls == [
        {"local_connector": "stoat", "local_user_id": "Bob", "destination": "discord"}
    ]
    assert user_linker.list_linked_users_calls == [{}]


async def test_linked_users_two_token_needs_no_admin_permission():
    user_linker = FakeUserLinker()
    sender = _make_sender(user_linker=user_linker)
    await sender._handle_message(_cmd_message("/linked users 01KH", manage_server=False))
    assert user_linker.list_linked_users_calls == [
        {"local_connector": "stoat", "local_user_id": "01KH"}
    ]


async def test_two_token_category_commands_route():
    category_linker = FakeCategoryLinker()
    sender = _make_sender(category_linker=category_linker)
    await sender._handle_message(_cmd_message("/link category discord src-cat dest-cat"))
    await sender._handle_message(_cmd_message("/unlink category MyCat all"))
    await sender._handle_message(_cmd_message("/linked categories MyCat"))
    await sender._handle_message(_cmd_message("/mirror category MyCat stoat"))
    await sender._handle_message(_cmd_message("/mirror category MyCat"))

    assert category_linker.link_category_calls[0]["source"] == "discord"
    assert category_linker.link_category_calls[0]["source_id"] == "src-cat"
    assert category_linker.link_category_calls[0]["destination_id"] == "dest-cat"
    assert category_linker.unlink_category_calls == [
        {"local_connector": "stoat", "local_category_id": None, "local_category": "MyCat", "destination": "all"}
    ]
    assert category_linker.list_linked_categories_calls == [
        {"local_connector": "stoat", "local_category_id": None, "local_category": "MyCat"}
    ]
    assert category_linker.mirror_category_calls == [
        {
            "local_connector": "stoat",
            "local_category_id": None,
            "local_category": "MyCat",
            "local_category_name": None,
            "destination": "stoat",
        }
    ]
    assert category_linker.mirror_category_all_calls == [
        {"local_connector": "stoat", "local_category_id": None, "local_category": "MyCat", "local_category_name": None}
    ]
