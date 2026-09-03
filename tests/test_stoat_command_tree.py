"""The `stoat.ext.commands` tree `_StoatClient` registers, and the
`process_commands` -> `_command_message_ids` -> `_handle_message` hand-off that
keeps a `/…` invocation (and its reply) from also being relayed as chat.
"""

from __future__ import annotations

from collections import deque
from types import SimpleNamespace

import stoat.ext.commands as stoat_commands

from stoat_discord_bridge.services.stoat_service import StoatSenderService, _StoatClient


def _bare_bot(owner=None, *, prefix: str = "/") -> _StoatClient:
    bot = object.__new__(_StoatClient)
    stoat_commands.Bot.__init__(bot, prefix)
    bot._owner = owner
    bot._prefix = prefix
    bot._state._me = SimpleNamespace(id="bridge-bot")
    bot._register_commands()
    return bot


def test_registers_the_four_groups_with_discord_matching_subcommands():
    bot = _bare_bot()
    assert sorted(bot.all_commands["link"].all_commands) == ["category", "channel", "emote", "role", "user"]
    assert sorted(bot.all_commands["unlink"].all_commands) == ["category", "channel", "emote", "role", "user"]
    assert sorted(bot.all_commands["linked"].all_commands) == [
        "categories",
        "channels",
        "emotes",
        "roles",
        "users",
    ]
    assert sorted(bot.all_commands["mirror"].all_commands) == ["category", "channel", "emote", "role"]
    # every `/mirror <noun>` is itself a to/from group
    for noun in ("category", "channel", "emote", "role"):
        assert sorted(bot.all_commands["mirror"].all_commands[noun].all_commands) == ["from", "to"]
    assert {"status", "bridge-help"} <= set(bot.all_commands)


class _FakeShard:
    pass


def _fake_message(content: str, *, message_id: str = "m1"):
    author = SimpleNamespace(bot=SimpleNamespace(), id="u1")  # a bot -> skip_check short-circuits before invoke
    return SimpleNamespace(
        content=content,
        id=message_id,
        author_id="u1",
        webhook=None,
        channel=SimpleNamespace(id="c1"),
        get_author=lambda: author,
    )


async def test_process_commands_records_a_recognised_command_message():
    owner = object.__new__(StoatSenderService)
    owner._command_message_ids = deque(maxlen=512)
    bot = _bare_bot(owner)

    await bot.process_commands(_fake_message("/link channel discord src", message_id="cmd-1"), _FakeShard())

    assert "cmd-1" in owner._command_message_ids


async def test_process_commands_ignores_a_non_command_message():
    owner = object.__new__(StoatSenderService)
    owner._command_message_ids = deque(maxlen=512)
    bot = _bare_bot(owner)

    await bot.process_commands(_fake_message("just chatting", message_id="chat-1"), _FakeShard())

    assert "chat-1" not in owner._command_message_ids


class _ReplyOwner:
    connector_id = "stoat"

    def __init__(self):
        self.replies = []

    async def _reply(self, ctx, text):
        self.replies.append(text)


async def test_on_command_error_reports_bad_usage():
    owner = _ReplyOwner()
    bot = _bare_bot(owner)
    ctx = SimpleNamespace(command=SimpleNamespace(qualified_name="link channel", signature="<service> <external_id>"))
    event = SimpleNamespace(error=stoat_commands.UserInputError("missing"), context=ctx)

    await bot.on_command_error(event)

    assert owner.replies == ["Usage: /link channel <service> <external_id>"]


async def test_on_command_error_usage_honours_a_custom_prefix():
    owner = _ReplyOwner()
    bot = _bare_bot(owner, prefix="!")
    ctx = SimpleNamespace(command=SimpleNamespace(qualified_name="link channel", signature="<service> <external_id>"))
    event = SimpleNamespace(error=stoat_commands.UserInputError("missing"), context=ctx)

    await bot.on_command_error(event)

    assert owner.replies == ["Usage: !link channel <service> <external_id>"]


async def test_group_usage_and_help_honour_a_custom_prefix():
    owner = _ReplyOwner()
    bot = _bare_bot(owner, prefix="!")

    await bot.all_commands["link"].callback(SimpleNamespace())
    await bot.all_commands["bridge-help"].callback(SimpleNamespace())

    assert owner.replies[0].startswith("Usage: !link ")
    assert "\n  !status - " in owner.replies[1]
    assert "/status" not in owner.replies[1]


async def test_on_command_error_ignores_command_not_found():
    owner = _ReplyOwner()
    bot = _bare_bot(owner)
    event = SimpleNamespace(
        error=stoat_commands.CommandNotFound("nope"), context=SimpleNamespace(command=None)
    )

    await bot.on_command_error(event)

    assert owner.replies == []


async def test_recorded_command_message_is_not_relayed():
    relayed = []
    owner = object.__new__(StoatSenderService)
    owner.connector_id = "stoat"
    owner._command_message_ids = deque(maxlen=512)
    owner._on_message = lambda m: relayed.append(m)

    message = SimpleNamespace(
        author=SimpleNamespace(bot=False, id="u1"),
        channel=SimpleNamespace(id="c1", name="general"),
        content="/status",
        id="cmd-9",
    )
    owner._command_message_ids.append("cmd-9")

    await StoatSenderService._handle_message(owner, message)

    assert relayed == []
