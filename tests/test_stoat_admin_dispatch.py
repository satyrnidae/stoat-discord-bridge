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

from stoat_discord_bridge.admin_commands import LinkError
from stoat_discord_bridge.channel_structure import ChannelSpec, GroupSpec, GuildStructure
from stoat_discord_bridge.services.stoat_service import StoatSenderService
from tests.fakes.fake_stoat import FakeChannel, FakeServer


class FakeLinker:
    def __init__(self, *, raises: LinkError | None = None) -> None:
        self._raises = raises
        self.link_channel_calls: list[dict] = []
        self.mirror_channel_calls: list[dict] = []
        self.mirror_channel_all_calls: list[dict] = []
        self.list_linked_channels_calls: list[dict] = []

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

    async def list_linked_users(self, **kwargs):
        self.list_linked_users_calls.append(kwargs)
        return "Linked users:\nDiscord: ShrinerH (216591124222050304) ↔ Stoat: shriner (01KH)"

    async def link_user(self, **kwargs):
        self.calls.append(kwargs)
        if self._raises is not None:
            raise self._raises
        return "user linked ok"


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


def _make_sender(
    *,
    linker: FakeLinker | None = None,
    mirrorer: FakeMirrorer | None = None,
    emote_linker: FakeEmoteLinker | None = None,
    user_linker: FakeUserLinker | None = None,
) -> StoatSenderService:
    sender = object.__new__(StoatSenderService)
    sender.connector_id = "stoat"
    sender._linker = linker
    sender._mirrorer = mirrorer
    sender._emote_linker = emote_linker
    sender._user_linker = user_linker
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
    sender = _make_sender(linker=FakeLinker(), emote_linker=FakeEmoteLinker(), user_linker=FakeUserLinker(), mirrorer=FakeMirrorer())
    message = _admin_message(manage_server=False)

    await sender._handle_mirror_channels(message, ["discord"])
    await sender._handle_link_channel(message, ["discord", "s1"])
    await sender._handle_link_emote(message, ["discord", "s1", "l1"])
    await sender._handle_link_user(message, ["discord", "u1", "l1"])
    await sender._handle_mirror_channel(message, ["discord"])

    assert message.channel.sent == [{"content": "You need the Manage Server permission to do that.", "masquerade": None}] * 5


# ---------------------------------------------------------------- _handle_link_channel


async def test_link_channel_success():
    linker = FakeLinker()
    sender = _make_sender(linker=linker)
    message = _admin_message(channel=FakeChannel(id="c1", name="general"))

    await sender._handle_link_channel(message, ["discord", "src-id", "dest-id"])

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

    assert message.channel.sent[0]["content"] == "Usage: /link-channel <source> <source_id> [<destination_id>]"


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

    assert message.channel.sent[0]["content"] == "Usage: /link-emote <source> <source_id> <local_id>"


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

    assert message.channel.sent[0]["content"] == "Usage: /link-user <source> <user_id> <local_user_id>"


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

    await sender._handle_mirror_channel(message, ["discord"])

    assert linker.mirror_channel_calls == [
        {"local_connector": "stoat", "local_channel_id": "c1", "local_channel_name": "general", "destination": "discord"}
    ]
    assert message.channel.sent[0]["content"] == "mirrored ok"


async def test_mirror_channel_to_all_is_case_insensitive():
    linker = FakeLinker()
    sender = _make_sender(linker=linker)
    message = _admin_message()

    await sender._handle_mirror_channel(message, ["ALL"])

    assert linker.mirror_channel_all_calls
    assert message.channel.sent[0]["content"] == "mirrored to all ok"


async def test_mirror_channel_uses_an_explicit_channel_id_when_given():
    linker = FakeLinker()
    sender = _make_sender(linker=linker)
    message = _admin_message()

    await sender._handle_mirror_channel(message, ["discord", "explicit-id"])

    call = linker.mirror_channel_calls[0]
    assert call["local_channel_id"] == "explicit-id"
    assert call["local_channel_name"] == "explicit-id"


async def test_mirror_channel_missing_destination_sends_usage():
    sender = _make_sender(linker=FakeLinker())
    message = _admin_message()

    await sender._handle_mirror_channel(message, [])

    assert message.channel.sent[0]["content"] == "Usage: /mirror-channel <destination|all> [local_channel_id]"


async def test_mirror_channel_without_a_configured_linker():
    sender = _make_sender(linker=None)
    message = _admin_message()

    await sender._handle_mirror_channel(message, ["discord"])

    assert message.channel.sent[0]["content"] == "Linking isn't configured."


# ---------------------------------------------------------------- _handle_mirror_channels


async def test_mirror_channels_success_creates_and_links():
    structure = GuildStructure(
        groups=[GroupSpec(name="Text", channels=[ChannelSpec(name="general", source_channel_id="d1")])],
        ungrouped_channels=[],
    )
    mirrorer = FakeMirrorer(structure=structure)
    linker = FakeLinker()
    sender = _make_sender(mirrorer=mirrorer, linker=linker)
    server = FakeServer(id="srv-1")
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

    assert message.channel.sent[0]["content"] == "Usage: /mirror-channels <source>"


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
