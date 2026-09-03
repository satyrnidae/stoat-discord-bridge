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


class _MirrorOwner:
    def __init__(self):
        self.mirror_role_calls = []
        self.mirror_emote_calls = []
        self.mirror_channel_calls = []
        self.mirror_channel_from_calls = []
        self.replies = []

    async def _reply(self, ctx, text):
        self.replies.append(text)

    async def _mirror_role(self, ctx, local_id=None, service=None, new_name=None):
        self.mirror_role_calls.append((local_id, service, new_name))

    async def _mirror_emote(self, ctx, local_id=None, service=None, new_name=None):
        self.mirror_emote_calls.append((local_id, service, new_name))

    async def _mirror_channel(self, ctx, local_id=None, service=None, new_name=None, category=None):
        self.mirror_channel_calls.append((local_id, service, new_name, category))

    async def _mirror_channel_from(self, ctx, service, external_id, new_name=None, category=None):
        self.mirror_channel_from_calls.append((service, external_id, new_name, category))


async def test_mirror_role_to_lone_arg_is_the_role_not_the_service():
    owner = _MirrorOwner()
    bot = _bare_bot(owner)
    to = bot.all_commands["mirror"].all_commands["role"].all_commands["to"]

    await to.callback(SimpleNamespace(), "Mods")
    await to.callback(SimpleNamespace(), "stoat", "Mods")
    await to.callback(SimpleNamespace(), "stoat", "Mods", "Moderators")

    assert owner.mirror_role_calls == [
        ("Mods", None, None),
        ("Mods", "stoat", None),
        ("Mods", "stoat", "Moderators"),
    ]


async def test_mirror_emote_to_lone_arg_is_the_emote_not_the_service():
    owner = _MirrorOwner()
    bot = _bare_bot(owner)
    to = bot.all_commands["mirror"].all_commands["emote"].all_commands["to"]

    await to.callback(SimpleNamespace(), "blob")
    await to.callback(SimpleNamespace(), "all", "blob")
    await to.callback(SimpleNamespace(), "stoat", "blob", "blobcat")

    assert owner.mirror_emote_calls == [
        ("blob", None, None),
        ("blob", "all", None),
        ("blob", "stoat", "blobcat"),
    ]


async def test_mirror_channel_to_pulls_a_category_kv_token_from_anywhere():
    owner = _MirrorOwner()
    bot = _bare_bot(owner)
    to = bot.all_commands["mirror"].all_commands["channel"].all_commands["to"]

    await to.callback(SimpleNamespace(), "stoat", "general")
    await to.callback(SimpleNamespace(), "stoat", "general", "category:01ABC")
    await to.callback(SimpleNamespace(), "stoat", "category:Bot Config", "general", "lobby")

    assert owner.mirror_channel_calls == [
        ("general", "stoat", None, None),
        ("general", "stoat", None, "01ABC"),
        ("general", "stoat", "lobby", "Bot Config"),
    ]


async def test_mirror_channel_from_pulls_a_category_kv_token_and_validates_arity():
    owner = _MirrorOwner()
    bot = _bare_bot(owner)
    frm = bot.all_commands["mirror"].all_commands["channel"].all_commands["from"]

    await frm.callback(SimpleNamespace(), "discord", "d1", "category:Team Beta")
    await frm.callback(SimpleNamespace(), "discord")

    assert owner.mirror_channel_from_calls == [("discord", "d1", None, "Team Beta")]
    assert owner.replies and "Usage:" in owner.replies[-1]


class _FakeShard:
    pass


def _fake_message(content: str, *, message_id: str = "m1", bot_author: bool = True):
    # bot_author=True -> skip_check short-circuits before invoke; set False to
    # exercise the real argument-parsing path.
    author = SimpleNamespace(bot=SimpleNamespace() if bot_author else None, id="u1")
    return SimpleNamespace(
        content=content,
        id=message_id,
        author_id="u1",
        webhook=None,
        attachments=[],
        channel=SimpleNamespace(id="c1"),
        get_author=lambda: author,
    )


class _OptionalArgOwner:
    connector_id = "stoat"

    def __init__(self):
        self.calls = []

    async def _reply(self, ctx, text):
        self.calls.append(("_reply", text))

    async def _linked_channels(self, ctx, local_id=None):
        self.calls.append(("_linked_channels", local_id))

    async def _link_channel(self, ctx, service, external_id, local_id=None):
        self.calls.append(("_link_channel", service, external_id, local_id))

    def _note_command_message(self, message_id):
        pass


async def test_command_with_optional_arg_omitted_parses_and_invokes():
    # Regression for issue #40: stoat.py 1.2.1's command framework raises
    # `TypeError: issubclass() arg 1 must be a class` on any `Optional[...]`
    # parameter unless `_compat.apply_stoat_command_patches` has run.
    owner = _OptionalArgOwner()
    bot = _bare_bot(owner)

    await bot.process_commands(_fake_message("/linked channels", bot_author=False), _FakeShard())

    assert owner.calls == [("_linked_channels", None)]


async def test_command_with_optional_arg_supplied_parses_and_invokes():
    owner = _OptionalArgOwner()
    bot = _bare_bot(owner)

    await bot.process_commands(
        _fake_message("/link channel discord 123 mychan", bot_author=False), _FakeShard()
    )

    assert owner.calls == [("_link_channel", "discord", "123", "mychan")]


def test_signature_of_a_command_with_an_optional_arg_renders():
    # `on_command_error` reads `Command.signature`; the same stoat.py bug hits
    # `issubclass(annotation, stoat.Asset)` there for an `Optional[...]` param.
    bot = _bare_bot()

    assert bot.all_commands["linked"].all_commands["channels"].signature == "[local_id]"
    assert (
        bot.all_commands["link"].all_commands["channel"].signature
        == "<service> <external_id> [local_id]"
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
