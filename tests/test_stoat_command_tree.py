"""The `stoat.ext.commands` tree `_StoatClient` registers, and the
`process_commands` -> `_command_message_ids` -> `_handle_message` hand-off that
keeps a `/…` invocation (and its reply) from also being relayed as chat.
"""

from __future__ import annotations

from collections import deque
from types import SimpleNamespace

import pytest
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
    assert sorted(bot.all_commands["unlink"].all_commands) == ["all", "category", "channel", "emote", "role", "user"]
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
    assert {"status", "bridge-help", "whitelist", "whitelisted", "import", "export"} <= set(bot.all_commands)


class _TransferOwner:
    def __init__(self):
        self.calls = []
        self.replies = []

    async def _reply(self, ctx, text):
        self.replies.append(text)

    async def _transfer_history(self, ctx, direction, service, external_channel, local_channel=None, limit=None):
        self.calls.append((direction, service, external_channel, local_channel, limit))


async def test_import_and_export_parse_a_limit_kv_token_from_anywhere():
    # issue #161
    owner = _TransferOwner()
    bot = _bare_bot(owner)

    await bot.all_commands["import"].callback(SimpleNamespace(), "discord", "general")
    await bot.all_commands["import"].callback(SimpleNamespace(), "discord", "general", "lobby", "limit:all")
    await bot.all_commands["export"].callback(SimpleNamespace(), "limit:20", "irc", "#chat")

    assert owner.calls == [
        ("import", "discord", "general", None, None),
        ("import", "discord", "general", "lobby", "all"),
        ("export", "irc", "#chat", None, "20"),
    ]


async def test_import_without_a_channel_replies_with_usage():
    owner = _TransferOwner()
    bot = _bare_bot(owner, prefix="!")

    await bot.all_commands["import"].callback(SimpleNamespace(), "discord")

    assert owner.calls == []
    assert owner.replies == ["Usage: !import <service> <external_channel> [local_channel] [limit:<n|all>]"]


class _MirrorOwner:
    def __init__(self):
        self.mirror_role_calls = []
        self.mirror_emote_calls = []
        self.mirror_channel_calls = []
        self.mirror_channel_from_calls = []
        self.mirror_category_calls = []
        self.mirror_from_calls = []
        self.replies = []

    async def _reply(self, ctx, text):
        self.replies.append(text)

    async def _mirror_role(self, ctx, service, local_id=None, new_name=None):
        self.mirror_role_calls.append((service, local_id, new_name))

    async def _mirror_emote(self, ctx, service, local_id=None, new_name=None):
        self.mirror_emote_calls.append((service, local_id, new_name))

    async def _mirror_category(self, ctx, service, local_id=None, new_name=None):
        self.mirror_category_calls.append((service, local_id, new_name))

    async def _mirror_role_from(self, ctx, service, external_id, new_name=None):
        self.mirror_from_calls.append(("role", service, external_id, new_name))

    async def _mirror_emote_from(self, ctx, service, external_id, new_name=None):
        self.mirror_from_calls.append(("emote", service, external_id, new_name))

    async def _mirror_category_from(self, ctx, service, external_id, new_name=None):
        self.mirror_from_calls.append(("category", service, external_id, new_name))

    def _note_command_message(self, message_id):
        pass

    async def _mirror_channel(
        self, ctx, service, local_id=None, new_name=None, category=None, with_history=False, history_limit=None
    ):
        self.mirror_channel_calls.append((service, local_id, new_name, category, with_history, history_limit))

    async def _mirror_channel_from(
        self, ctx, service, external_id, new_name=None, category=None, with_history=False, history_limit=None
    ):
        self.mirror_channel_from_calls.append(
            (service, external_id, new_name, category, with_history, history_limit)
        )


async def test_mirror_role_to_takes_service_then_role():
    # issue #97: `<service|all>` is a required leading argument now - no
    # lone-arg heuristic to resolve.
    owner = _MirrorOwner()
    bot = _bare_bot(owner)
    to = bot.all_commands["mirror"].all_commands["role"].all_commands["to"]

    await to.callback(SimpleNamespace(), "all", "Mods")
    await to.callback(SimpleNamespace(), "stoat", "Mods")
    await to.callback(SimpleNamespace(), "stoat", "Mods", "new_name:Moderators")
    await to.callback(SimpleNamespace(), "new_name:Moderators", "stoat", "Mods")

    assert owner.mirror_role_calls == [
        ("all", "Mods", None),
        ("stoat", "Mods", None),
        ("stoat", "Mods", "Moderators"),
        ("stoat", "Mods", "Moderators"),
    ]


async def test_mirror_role_and_emote_to_declare_a_required_service():
    # issue #97: a lone `to <role>` no longer silently fans out - `<service>`
    # is a required positional, so stoat.py's framework reports it missing
    # (surfaced as a Usage message by `on_command_error`, covered elsewhere).
    bot = _bare_bot()
    for noun in ("role", "emote"):
        to = bot.all_commands["mirror"].all_commands[noun].all_commands["to"]
        assert to.signature == "<service> <local_id> [options...]"


@pytest.mark.parametrize("noun", ["role", "emote", "category"])
async def test_mirror_to_rejects_a_bare_trailing_new_name(noun):
    # issue #167: new_name is `new_name:<value>` now, not a positional - a bare
    # extra token is a usage error rather than silently becoming the new name.
    owner = _MirrorOwner()
    bot = _bare_bot(owner)
    to = bot.all_commands["mirror"].all_commands[noun].all_commands["to"]

    await to.callback(SimpleNamespace(), "stoat", "thing", "Renamed")

    assert getattr(owner, f"mirror_{noun}_calls") == []
    assert owner.replies and "new_name:<name>" in owner.replies[-1]


async def test_mirror_category_to_takes_a_new_name_token_and_optional_local_id():
    owner = _MirrorOwner()
    bot = _bare_bot(owner)
    to = bot.all_commands["mirror"].all_commands["category"].all_commands["to"]

    await to.callback(SimpleNamespace(), "stoat")
    await to.callback(SimpleNamespace(), "stoat", "new_name:Lounge")
    await to.callback(SimpleNamespace(), "stoat", "Games", 'new_name:"Game', 'Room"')

    assert owner.mirror_category_calls == [
        ("stoat", None, None),
        ("stoat", None, "Lounge"),
        ("stoat", "Games", "Game Room"),
    ]


@pytest.mark.parametrize("noun", ["role", "emote", "category"])
async def test_mirror_from_takes_a_new_name_token(noun):
    owner = _MirrorOwner()
    bot = _bare_bot(owner)
    frm = bot.all_commands["mirror"].all_commands[noun].all_commands["from"]

    await frm.callback(SimpleNamespace(), "discord", "d1")
    await frm.callback(SimpleNamespace(), "discord", "d1", "NEW_NAME:Local")
    await frm.callback(SimpleNamespace(), "discord", "d1", "Local")

    assert owner.mirror_from_calls == [(noun, "discord", "d1", None), (noun, "discord", "d1", "Local")]
    assert len(owner.replies) == 1 and "new_name:<name>" in owner.replies[0]


async def test_mirror_emote_to_takes_service_then_emote():
    owner = _MirrorOwner()
    bot = _bare_bot(owner)
    to = bot.all_commands["mirror"].all_commands["emote"].all_commands["to"]

    await to.callback(SimpleNamespace(), "all", "blob")
    await to.callback(SimpleNamespace(), "stoat", "blob", "new_name:blobcat")

    assert owner.mirror_emote_calls == [
        ("all", "blob", None),
        ("stoat", "blob", "blobcat"),
    ]


async def test_mirror_channel_to_pulls_a_category_kv_token_from_anywhere():
    owner = _MirrorOwner()
    bot = _bare_bot(owner)
    to = bot.all_commands["mirror"].all_commands["channel"].all_commands["to"]

    await to.callback(SimpleNamespace(), "stoat", "general")
    await to.callback(SimpleNamespace(), "stoat", "general", "category:01ABC")
    await to.callback(SimpleNamespace(), "stoat", "category:Bot Config", "general", "new_name:lobby")
    await to.callback(SimpleNamespace(), "stoat", "new_name:lobby", "history", "category:X")

    assert owner.mirror_channel_calls == [
        ("stoat", "general", None, None, False, None),
        ("stoat", "general", None, "01ABC", False, None),
        ("stoat", "general", "lobby", "Bot Config", False, None),
        ("stoat", None, "lobby", "X", True, None),
    ]


async def test_mirror_channel_to_rejects_a_bare_trailing_new_name():
    owner = _MirrorOwner()
    bot = _bare_bot(owner)
    to = bot.all_commands["mirror"].all_commands["channel"].all_commands["to"]

    await to.callback(SimpleNamespace(), "stoat", "general", "lobby")

    assert owner.mirror_channel_calls == []
    assert owner.replies and "new_name:<name>" in owner.replies[-1]


async def test_mirror_channel_from_takes_a_new_name_token():
    owner = _MirrorOwner()
    bot = _bare_bot(owner)
    frm = bot.all_commands["mirror"].all_commands["channel"].all_commands["from"]

    await frm.callback(SimpleNamespace(), "discord", "d1", "new_name:lobby", "history:5")
    await frm.callback(SimpleNamespace(), "discord", "d1", "lobby")

    assert owner.mirror_channel_from_calls == [("discord", "d1", "lobby", None, True, "5")]
    assert owner.replies and "Usage:" in owner.replies[-1]


async def test_mirror_channel_to_declares_a_required_service():
    # issue #97: `<service>` is a required positional now - stoat.py's framework
    # reports it missing when omitted entirely.
    bot = _bare_bot()
    to = bot.all_commands["mirror"].all_commands["channel"].all_commands["to"]

    assert to.signature == "<service> [local_id] [options...]"


async def test_mirror_channel_to_with_only_a_category_token_replies_usage():
    # the one case the framework can't catch: `service` was bound but is really
    # the `category:` kv token, leaving no positional tokens -> Usage, not a
    # fan-out to every connector.
    owner = _MirrorOwner()
    bot = _bare_bot(owner)
    to = bot.all_commands["mirror"].all_commands["channel"].all_commands["to"]

    await to.callback(SimpleNamespace(), "category:01ABC")

    assert owner.mirror_channel_calls == []
    assert owner.replies == [
        "Usage: /mirror channel to <service|all> [local_id|name] [new_name:<name>] "
        "[category:<id|name>] [history[:<n|all>]]"
    ]


async def test_mirror_channel_from_pulls_a_category_kv_token_and_validates_arity():
    owner = _MirrorOwner()
    bot = _bare_bot(owner)
    frm = bot.all_commands["mirror"].all_commands["channel"].all_commands["from"]

    await frm.callback(SimpleNamespace(), "discord", "d1", "category:Team Beta")
    await frm.callback(SimpleNamespace(), "discord")

    assert owner.mirror_channel_from_calls == [("discord", "d1", None, "Team Beta", False, None)]
    assert owner.replies and "Usage:" in owner.replies[-1]


async def test_mirror_channel_to_pulls_a_history_kv_token_from_anywhere():
    owner = _MirrorOwner()
    bot = _bare_bot(owner)
    to = bot.all_commands["mirror"].all_commands["channel"].all_commands["to"]

    await to.callback(SimpleNamespace(), "stoat", "general", "history:100")
    await to.callback(SimpleNamespace(), "stoat", "general", "history:all")
    await to.callback(SimpleNamespace(), "stoat", "general", "history")

    assert owner.mirror_channel_calls == [
        ("stoat", "general", None, None, True, "100"),
        ("stoat", "general", None, None, True, "all"),
        ("stoat", "general", None, None, True, None),
    ]


async def test_mirror_channel_from_forwards_a_history_kv_token():
    owner = _MirrorOwner()
    bot = _bare_bot(owner)
    frm = bot.all_commands["mirror"].all_commands["channel"].all_commands["from"]

    await frm.callback(SimpleNamespace(), "discord", "d1", "history:25")

    assert owner.mirror_channel_from_calls == [("discord", "d1", None, None, True, "25")]


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


async def test_mirror_named_options_parse_through_the_real_argument_parser():
    # stoat.py rejects a quote that starts mid-word (`new_name:"Main Hall"`),
    # so a multi-word value quotes the whole token instead.
    owner = _MirrorOwner()
    bot = _bare_bot(owner)

    await bot.process_commands(
        _fake_message('/mirror channel to discord general "new_name:Main Hall" history', bot_author=False),
        _FakeShard(),
    )
    await bot.process_commands(
        _fake_message("/mirror role to discord Mods new_name:Moderators", bot_author=False), _FakeShard()
    )

    assert owner.mirror_channel_calls == [("discord", "general", "Main Hall", None, True, None)]
    assert owner.mirror_role_calls == [("discord", "Mods", "Moderators")]


def test_signature_of_a_command_with_an_optional_arg_renders():
    # `on_command_error` reads `Command.signature`; the same stoat.py bug hits
    # `issubclass(annotation, stoat.Asset)` there for an `Optional[...]` param.
    bot = _bare_bot()

    assert bot.all_commands["linked"].all_commands["channels"].signature == "[local_id]"
    assert (
        bot.all_commands["link"].all_commands["channel"].signature
        == "<service> <external_id> [local_id]"
    )


async def test_process_commands_records_a_recognized_command_message():
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


async def test_on_command_error_usage_honors_a_custom_prefix():
    owner = _ReplyOwner()
    bot = _bare_bot(owner, prefix="!")
    ctx = SimpleNamespace(command=SimpleNamespace(qualified_name="link channel", signature="<service> <external_id>"))
    event = SimpleNamespace(error=stoat_commands.UserInputError("missing"), context=ctx)

    await bot.on_command_error(event)

    assert owner.replies == ["Usage: !link channel <service> <external_id>"]


async def test_group_usage_and_help_honor_a_custom_prefix():
    owner = _ReplyOwner()
    bot = _bare_bot(owner, prefix="!")

    await bot.all_commands["link"].callback(SimpleNamespace())
    await bot.all_commands["bridge-help"].callback(SimpleNamespace())

    assert owner.replies[0].startswith("Usage: !link ")
    assert "\n  !status - " in owner.replies[1]
    assert "/status" not in owner.replies[1]


async def test_bridge_help_with_a_topic_and_noun_drills_down():
    owner = _ReplyOwner()
    bot = _bare_bot(owner, prefix="!")

    await bot.all_commands["bridge-help"].callback(SimpleNamespace(), "mirror", "channel")

    assert owner.replies[0].startswith("!mirror channel to")


async def test_bridge_help_with_an_unrecognized_topic_falls_back_to_the_index():
    owner = _ReplyOwner()
    bot = _bare_bot(owner)

    await bot.all_commands["bridge-help"].callback(SimpleNamespace(), "not-a-real-topic")

    assert owner.replies[0].startswith("Bridge commands (see COMMANDS.md for full detail):")


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


# ---------------------------------------------------------------- /whitelist, /whitelisted


class _WhitelistOwner:
    connector_id = "stoat"

    def __init__(self, connectors: dict | None = None):
        self.replies = []
        self.whitelist_calls = []
        self.whitelisted_calls = []
        self._bot_whitelist = SimpleNamespace(connectors=connectors or {})

    async def _reply(self, ctx, text):
        self.replies.append(text)

    async def _whitelist(self, ctx, action, target, bot_ref):
        self.whitelist_calls.append((action, target, bot_ref))

    async def _whitelisted(self, ctx, target):
        self.whitelisted_calls.append(target)


async def test_whitelist_with_no_args_replies_usage():
    owner = _WhitelistOwner()
    bot = _bare_bot(owner)

    await bot.all_commands["whitelist"].callback(SimpleNamespace())

    assert owner.replies == ["Usage: /whitelist [add|remove] [local|<service>] <bot_id|name>"]
    assert owner.whitelist_calls == []


async def test_whitelist_defaults_to_add_and_local_with_only_a_bot_ref():
    owner = _WhitelistOwner()
    bot = _bare_bot(owner)

    await bot.all_commands["whitelist"].callback(SimpleNamespace(), "bot1")

    assert owner.whitelist_calls == [("add", "local", "bot1")]


async def test_whitelist_parses_a_leading_action():
    owner = _WhitelistOwner()
    bot = _bare_bot(owner)

    await bot.all_commands["whitelist"].callback(SimpleNamespace(), "remove", "bot1")

    assert owner.whitelist_calls == [("remove", "local", "bot1")]


async def test_whitelist_parses_a_known_connector_as_the_target():
    owner = _WhitelistOwner({"discord": SimpleNamespace()})
    bot = _bare_bot(owner)

    await bot.all_commands["whitelist"].callback(SimpleNamespace(), "add", "discord", "bot1")

    assert owner.whitelist_calls == [("add", "discord", "bot1")]


async def test_whitelist_treats_an_unknown_token_as_part_of_the_bot_ref():
    # "WebhookBot" isn't "local" or a known connector id, so it's not
    # mistaken for the target - it (and any further tokens) become the
    # bot_ref, joined with a space.
    owner = _WhitelistOwner({"discord": SimpleNamespace()})
    bot = _bare_bot(owner)

    await bot.all_commands["whitelist"].callback(SimpleNamespace(), "Webhook", "Bot")

    assert owner.whitelist_calls == [("add", "local", "Webhook Bot")]


async def test_whitelist_action_and_target_but_no_bot_ref_replies_usage():
    owner = _WhitelistOwner({"discord": SimpleNamespace()})
    bot = _bare_bot(owner)

    await bot.all_commands["whitelist"].callback(SimpleNamespace(), "add", "discord")

    assert owner.replies == ["Usage: /whitelist [add|remove] [local|<service>] <bot_id|name>"]
    assert owner.whitelist_calls == []


async def test_whitelisted_defaults_to_local():
    owner = _WhitelistOwner()
    bot = _bare_bot(owner)

    await bot.all_commands["whitelisted"].callback(SimpleNamespace())

    assert owner.whitelisted_calls == ["local"]


async def test_whitelisted_takes_an_explicit_service():
    owner = _WhitelistOwner()
    bot = _bare_bot(owner)

    await bot.all_commands["whitelisted"].callback(SimpleNamespace(), "discord")

    assert owner.whitelisted_calls == ["discord"]


# ---------------------------------------------------------------- /attachments (issue #164)


class _AttachmentsOwner:
    def __init__(self):
        self.replies = []
        self.calls = []

    async def _reply(self, ctx, text):
        self.replies.append(text)

    async def _attachments_prefer(self, ctx, kind, url_substring):
        self.calls.append(("prefer", kind, url_substring))

    async def _attachments_unprefer(self, ctx, url_substring):
        self.calls.append(("unprefer", url_substring))

    async def _attachments_preferences(self, ctx):
        self.calls.append(("preferences",))


def test_registers_the_attachments_group():
    bot = _bare_bot()
    assert sorted(bot.all_commands["attachments"].all_commands) == ["prefer", "preferences", "unprefer"]


async def test_attachments_subcommands_forward_to_the_owner():
    owner = _AttachmentsOwner()
    group = _bare_bot(owner).all_commands["attachments"]

    await group.all_commands["prefer"].callback(SimpleNamespace(), "stoat", "instagram.com")
    await group.all_commands["unprefer"].callback(SimpleNamespace(), "instagram.com")
    await group.all_commands["preferences"].callback(SimpleNamespace())

    assert owner.calls == [
        ("prefer", "stoat", "instagram.com"),
        ("unprefer", "instagram.com"),
        ("preferences",),
    ]


async def test_attachments_with_no_subcommand_replies_usage():
    owner = _AttachmentsOwner()
    bot = _bare_bot(owner)

    await bot.all_commands["attachments"].callback(SimpleNamespace())

    assert owner.replies == ["Usage: /attachments <prefer <discord|stoat> <url-substr>|unprefer <url-substr>|preferences>"]
