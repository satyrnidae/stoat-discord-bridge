"""Tests for IrcSenderService's DM-command surface - _handle_privmsg's
routing (STATUS vs. the oper-gated LINK CHANNEL / MIRROR CHANNEL /
UNLINK CHANNEL / LINK_EMOTE / LINK_USER admin commands) and _handle_dm_command's command bodies.
Previously untested (0% coverage on this whole code path).

The oper check itself (_check_is_oper/_resolve_whois, the WHOIS Future
dance) is already thoroughly covered in test_irc_service.py - these tests
monkeypatch it to a plain bool so the command-body logic can be tested in
isolation from that.
"""

from __future__ import annotations

import asyncio

import pytest

from stoat_discord_bridge.admin_commands import LinkError
from stoat_discord_bridge.config import IrcConnectorConfig
from stoat_discord_bridge.services.irc_service import IrcSenderService
from stoat_discord_bridge.status import HealthTracker
from tests.fakes.fake_irc import FakeIrcConnection, FakeIrcEvent


def _irc_config(**overrides):
    defaults = dict(
        id="irc", label="IRC", host="irc.example.invalid", port=6697, use_tls=True, nick="bot",
        nickserv_password=None, default_channel_modes=None, ident=None, oper_account=None, oper_password=None,
        client_cert_file=None, client_key_file=None,
    )
    defaults.update(overrides)
    return IrcConnectorConfig(**defaults)


async def _noop(_message) -> None:
    pass


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
        return "Linked channels:\nIRC: #general (#general) (this channel)"

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

    async def link_user(self, **kwargs):
        self.calls.append(kwargs)
        if self._raises is not None:
            raise self._raises
        return "user linked ok"

    async def list_linked_users(self, **kwargs):
        self.list_linked_users_calls.append(kwargs)
        return "Linked users:\nDiscord: ShrinerH (216591124222050304) ↔ Stoat: shriner (01KH)"

    async def unlink_user(self, **kwargs):
        self.unlink_user_calls.append(kwargs)
        if self._raises is not None:
            raise self._raises
        return "user unlinked ok"


def _make_sender(
    *,
    is_oper: bool = True,
    linker: FakeLinker | None = None,
    emote_linker: FakeEmoteLinker | None = None,
    user_linker: FakeUserLinker | None = None,
    **config_overrides,
) -> tuple[IrcSenderService, FakeIrcConnection]:
    sender = IrcSenderService(
        _irc_config(**config_overrides),
        [],
        on_message=_noop,
        health=HealthTracker({"irc": "IRC"}),
        linker=linker,
        emote_linker=emote_linker,
        user_linker=user_linker,
    )
    sender._loop = asyncio.get_running_loop()
    conn = FakeIrcConnection()
    sender._client.connection = conn

    async def fake_check_is_oper(_nick: str) -> bool:
        return is_oper

    sender._check_is_oper = fake_check_is_oper
    return sender, conn


# ---------------------------------------------------------------- _handle_privmsg routing


async def test_privmsg_status_replies_directly_without_scheduling():
    sender, conn = _make_sender()
    scheduled = []
    sender._schedule = lambda coro: scheduled.append(coro)

    sender._handle_privmsg(conn, FakeIrcEvent(text="STATUS", nick="alice"))

    assert scheduled == []
    assert conn.notice_calls  # health.render() splitlines, at least one line
    assert conn.notice_calls[0][0] == "alice"


async def test_privmsg_status_is_case_insensitive_and_tolerates_whitespace():
    sender, conn = _make_sender()
    sender._handle_privmsg(conn, FakeIrcEvent(text="  status  ", nick="alice"))
    assert conn.notice_calls


async def test_privmsg_unrecognized_text_is_ignored():
    sender, _conn = _make_sender()
    scheduled = []
    sender._schedule = lambda coro: scheduled.append(coro)

    sender._handle_privmsg(None, FakeIrcEvent(text="just chatting", nick="alice"))

    assert scheduled == []


async def test_privmsg_admin_command_is_scheduled_not_run_inline():
    sender, _conn = _make_sender()
    scheduled = []
    sender._schedule = lambda coro: scheduled.append(coro)

    sender._handle_privmsg(None, FakeIrcEvent(text="LINK CHANNEL a b c", nick="alice"))

    assert len(scheduled) == 1
    scheduled[0].close()  # never awaited - avoid a "coroutine was never awaited" warning


# ---------------------------------------------------------------- _handle_dm_command: oper gate


async def test_dm_command_rejects_a_non_oper():
    sender, conn = _make_sender(is_oper=False, linker=FakeLinker())

    await sender._handle_dm_command("alice", "LINK CHANNEL 1 discord 2")

    assert conn.notice_calls == [("alice", "You need to be an IRC operator to do that.")]


# ---------------------------------------------------------------- LINK_CHANNEL


async def test_link_channel_success():
    linker = FakeLinker()
    sender, conn = _make_sender(linker=linker)

    await sender._handle_dm_command("alice", "LINK CHANNEL local-id discord src-id")

    assert linker.link_channel_calls == [
        {
            "local_connector": "irc",
            "local_channel_id": "local-id",
            "local_channel_name": "local-id",
            "source": "discord",
            "source_id": "src-id",
            "destination_id": "local-id",
        }
    ]
    assert conn.notice_calls == [("alice", "linked ok")]


async def test_link_channel_wrong_arg_count_sends_usage():
    sender, conn = _make_sender(linker=FakeLinker())

    await sender._handle_dm_command("alice", "LINK CHANNEL discord src-id")

    assert conn.notice_calls == [("alice", "Usage: LINK CHANNEL <local_id> <service> <external_id>")]


async def test_link_channel_without_a_configured_linker():
    sender, conn = _make_sender(linker=None)

    await sender._handle_dm_command("alice", "LINK CHANNEL local-id discord src-id")

    assert conn.notice_calls == [("alice", "Linking isn't configured.")]


async def test_link_channel_reports_a_link_error():
    sender, conn = _make_sender(linker=FakeLinker(raises=LinkError("already linked elsewhere")))

    await sender._handle_dm_command("alice", "LINK CHANNEL local-id discord src-id")

    assert conn.notice_calls == [("alice", "already linked elsewhere")]


# ---------------------------------------------------------------- LINK_EMOTE


async def test_link_emote_success():
    emote_linker = FakeEmoteLinker()
    sender, conn = _make_sender(emote_linker=emote_linker)

    await sender._handle_dm_command("alice", "LINK_EMOTE discord src-id local-id")

    assert emote_linker.calls == [{"local_connector": "irc", "local_id": "local-id", "source": "discord", "source_id": "src-id"}]
    assert conn.notice_calls == [("alice", "emote linked ok")]


async def test_link_emote_without_a_configured_linker():
    sender, conn = _make_sender(emote_linker=None)

    await sender._handle_dm_command("alice", "LINK_EMOTE discord src-id local-id")

    assert conn.notice_calls == [("alice", "Linking isn't configured.")]


# ---------------------------------------------------------------- LINK_USER


async def test_link_user_success():
    user_linker = FakeUserLinker()
    sender, conn = _make_sender(user_linker=user_linker)

    await sender._handle_dm_command("alice", "LINK_USER discord remote-id local-id")

    assert user_linker.calls == [
        {"local_connector": "irc", "local_user_id": "local-id", "source": "discord", "source_user_id": "remote-id"}
    ]
    assert conn.notice_calls == [("alice", "user linked ok")]


async def test_link_user_without_a_configured_linker():
    sender, conn = _make_sender(user_linker=None)

    await sender._handle_dm_command("alice", "LINK_USER discord remote-id local-id")

    assert conn.notice_calls == [("alice", "User linking isn't configured.")]


# ---------------------------------------------------------------- MIRROR_CHANNEL


async def test_mirror_channel_to_a_single_destination():
    linker = FakeLinker()
    sender, conn = _make_sender(linker=linker)

    await sender._handle_dm_command("alice", "MIRROR CHANNEL #general discord")

    assert linker.mirror_channel_calls == [
        {"local_connector": "irc", "local_channel_id": "#general", "local_channel_name": "#general", "destination": "discord"}
    ]
    assert conn.notice_calls == [("alice", "mirrored ok")]


async def test_mirror_channel_to_all_is_case_insensitive():
    linker = FakeLinker()
    sender, conn = _make_sender(linker=linker)

    await sender._handle_dm_command("alice", "MIRROR CHANNEL #general ALL")

    assert linker.mirror_channel_all_calls == [
        {"local_connector": "irc", "local_channel_id": "#general", "local_channel_name": "#general"}
    ]
    assert conn.notice_calls == [("alice", "mirrored to all ok")]


async def test_mirror_channel_wrong_arg_count_sends_usage():
    sender, conn = _make_sender(linker=FakeLinker())

    await sender._handle_dm_command("alice", "MIRROR CHANNEL a b c")

    assert conn.notice_calls == [("alice", "Usage: MIRROR CHANNEL <local_id> [service|all]")]


# ---------------------------------------------------------------- unrecognized command


async def test_unrecognized_admin_command_is_silently_ignored():
    sender, conn = _make_sender(linker=FakeLinker())

    await sender._handle_dm_command("alice", "NOT_A_REAL_COMMAND foo")

    assert conn.notice_calls == []


# ---------------------------------------------------------------- LINKED_CHANNELS


async def test_privmsg_linked_channels_is_scheduled_not_run_inline():
    sender, _conn = _make_sender(linker=FakeLinker())
    scheduled = []
    sender._schedule = lambda coro: scheduled.append(coro)

    sender._handle_privmsg(None, FakeIrcEvent(text="LINKED CHANNELS #general", nick="alice"))

    assert len(scheduled) == 1
    scheduled[0].close()


async def test_linked_channels_reports_the_requested_channel():
    linker = FakeLinker()
    sender, conn = _make_sender(linker=linker)

    await sender._handle_linked_channels_command("alice", ["#general"])

    assert linker.list_linked_channels_calls == [{"local_connector": "irc", "local_channel_id": "#general"}]
    assert conn.notice_calls == [("alice", "Linked channels:"), ("alice", "IRC: #general (#general) (this channel)")]


async def test_linked_channels_wrong_arg_count_sends_usage():
    sender, conn = _make_sender(linker=FakeLinker())

    await sender._handle_linked_channels_command("alice", [])

    assert conn.notice_calls == [("alice", "Usage: LINKED CHANNELS <local_id>")]


async def test_linked_channels_without_a_configured_linker():
    sender, conn = _make_sender(linker=None)

    await sender._handle_linked_channels_command("alice", ["#general"])

    assert conn.notice_calls == [("alice", "Linking isn't configured.")]


async def test_linked_channels_needs_no_oper_status():
    linker = FakeLinker()
    sender, _conn = _make_sender(is_oper=False, linker=linker)

    await sender._handle_linked_channels_command("alice", ["#general"])  # must not be rejected

    assert linker.list_linked_channels_calls


# ---------------------------------------------------------------- LINKED_USERS


async def test_privmsg_linked_users_is_scheduled_not_run_inline():
    sender, _conn = _make_sender(user_linker=FakeUserLinker())
    scheduled = []
    sender._schedule = lambda coro: scheduled.append(coro)

    sender._handle_privmsg(None, FakeIrcEvent(text="LINKED_USERS 01KH", nick="alice"))

    assert len(scheduled) == 1
    scheduled[0].close()


async def test_linked_users_with_an_argument_shows_only_that_users_link():
    user_linker = FakeUserLinker()
    sender, conn = _make_sender(user_linker=user_linker)

    await sender._handle_linked_users_command("alice", ["01KH7TH31EBY08FTQ7YC2RC4DQ"])

    assert user_linker.list_linked_users_calls == [
        {"local_connector": "irc", "local_user_id": "01KH7TH31EBY08FTQ7YC2RC4DQ"}
    ]
    assert conn.notice_calls == [
        ("alice", "Linked users:"),
        ("alice", "Discord: ShrinerH (216591124222050304) ↔ Stoat: shriner (01KH)"),
    ]


async def test_linked_users_with_no_argument_lists_everything():
    user_linker = FakeUserLinker()
    sender, _conn = _make_sender(user_linker=user_linker)

    await sender._handle_linked_users_command("alice", [])

    assert user_linker.list_linked_users_calls == [{}]


async def test_linked_users_without_a_configured_user_linker():
    sender, conn = _make_sender(user_linker=None)

    await sender._handle_linked_users_command("alice", [])

    assert conn.notice_calls == [("alice", "Linking isn't configured.")]


async def test_linked_users_needs_no_oper_status():
    user_linker = FakeUserLinker()
    sender, _conn = _make_sender(is_oper=False, user_linker=user_linker)

    await sender._handle_linked_users_command("alice", [])  # must not be rejected

    assert user_linker.list_linked_users_calls


# ---------------------------------------------------------------- UNLINK_CHANNEL


async def test_unlink_channel_defaults_destination_to_none():
    linker = FakeLinker()
    sender, conn = _make_sender(linker=linker)

    await sender._handle_dm_command("alice", "UNLINK CHANNEL #general")

    assert linker.unlink_channel_calls == [
        {"local_connector": "irc", "local_channel_id": "#general", "destination": None}
    ]
    assert conn.notice_calls == [("alice", "unlinked ok")]


async def test_unlink_channel_with_a_specific_destination():
    linker = FakeLinker()
    sender, conn = _make_sender(linker=linker)

    await sender._handle_dm_command("alice", "UNLINK CHANNEL #general discord")

    assert linker.unlink_channel_calls == [
        {"local_connector": "irc", "local_channel_id": "#general", "destination": "discord"}
    ]


async def test_unlink_channel_wrong_arg_count_sends_usage():
    sender, conn = _make_sender(linker=FakeLinker())

    await sender._handle_dm_command("alice", "UNLINK CHANNEL")

    assert conn.notice_calls == [("alice", "Usage: UNLINK CHANNEL <local_id> [service|all]")]


async def test_unlink_channel_without_a_configured_linker():
    sender, conn = _make_sender(linker=None)

    await sender._handle_dm_command("alice", "UNLINK CHANNEL #general")

    assert conn.notice_calls == [("alice", "Linking isn't configured.")]


async def test_unlink_channel_rejects_a_non_oper():
    sender, conn = _make_sender(is_oper=False, linker=FakeLinker())

    await sender._handle_dm_command("alice", "UNLINK CHANNEL #general")

    assert conn.notice_calls == [("alice", "You need to be an IRC operator to do that.")]


# ---------------------------------------------------------------- UNLINK_USER


async def test_unlink_user_defaults_to_all_and_self():
    user_linker = FakeUserLinker()
    sender, conn = _make_sender(user_linker=user_linker)

    await sender._handle_dm_command("alice", "UNLINK_USER")

    assert user_linker.unlink_user_calls == [{"local_connector": "irc", "local_user_id": "alice", "destination": None}]
    assert conn.notice_calls == [("alice", "user unlinked ok")]


async def test_unlink_user_with_a_specific_destination_and_target():
    user_linker = FakeUserLinker()
    sender, conn = _make_sender(user_linker=user_linker)

    await sender._handle_dm_command("alice", "UNLINK_USER discord bob")

    assert user_linker.unlink_user_calls == [{"local_connector": "irc", "local_user_id": "bob", "destination": "discord"}]


async def test_unlink_user_too_many_args_sends_usage():
    sender, conn = _make_sender(user_linker=FakeUserLinker())

    await sender._handle_dm_command("alice", "UNLINK_USER discord bob extra")

    assert conn.notice_calls == [("alice", "Usage: UNLINK_USER [service|all] [local_id]")]


async def test_unlink_user_without_a_configured_linker():
    sender, conn = _make_sender(user_linker=None)

    await sender._handle_dm_command("alice", "UNLINK_USER")

    assert conn.notice_calls == [("alice", "User linking isn't configured.")]


async def test_unlink_user_rejects_a_non_oper():
    sender, conn = _make_sender(is_oper=False, user_linker=FakeUserLinker())

    await sender._handle_dm_command("alice", "UNLINK_USER")

    assert conn.notice_calls == [("alice", "You need to be an IRC operator to do that.")]


# ---------------------------------------------------------------- HELP


async def test_privmsg_help_replies_directly_without_scheduling():
    sender, conn = _make_sender()
    scheduled = []
    sender._schedule = lambda coro: scheduled.append(coro)

    sender._handle_privmsg(None, FakeIrcEvent(text="HELP", nick="alice"))

    assert scheduled == []
    assert conn.notice_calls
    assert conn.notice_calls[0][0] == "alice"


async def test_privmsg_help_is_case_insensitive():
    sender, conn = _make_sender()
    sender._handle_privmsg(None, FakeIrcEvent(text="  help  ", nick="alice"))
    assert conn.notice_calls


async def test_privmsg_help_needs_no_oper_status():
    sender, conn = _make_sender(is_oper=False)
    sender._handle_privmsg(None, FakeIrcEvent(text="HELP", nick="alice"))
    assert conn.notice_calls
    assert "operator" not in conn.notice_calls[0][1].lower()
