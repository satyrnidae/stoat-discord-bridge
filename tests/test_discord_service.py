"""Tests for the pieces of DiscordSenderService that don't require an actual
Discord connection - constructing one only builds a discord.Client/
CommandTree in memory (no network) the same way IrcSenderService's
constructor doesn't touch a socket, so a real instance is safe to build here.

Covers _normalize_channel_id and its use in the /link-channel and
/mirror-channel handlers: a real bug where pasting a Discord channel mention
(the `<#id>` text the client inserts when a user picks a channel from the
"#" autocomplete inside a plain string slash-command option) into
/mirror-channel's `local_channel_id` caused a Stoat channel literally named
"<#814279082606592020>" to get created, because the mention text flowed
straight through as both the channel id *and*, since no id-based name
lookup was attempted, the display name handed to the other connector's
ensure_channel().
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from stoat_discord_bridge.admin_commands import ConnectorInfo
from stoat_discord_bridge.config import DiscordConnectorConfig
from stoat_discord_bridge.services.discord_service import (
    DiscordSenderService,
    _connector_autocomplete_choices,
    _normalize_channel_id,
)
from stoat_discord_bridge.status import HealthTracker
from tests.fakes.fake_discord import FakeGuild, FakeGuildChannel


def _discord_config(**overrides):
    defaults = dict(id="discord", label="Discord", guild_id=123, bot_token="fake-token")
    defaults.update(overrides)
    return DiscordConnectorConfig(**defaults)


async def _noop(_message) -> None:
    pass


class FakeLinker:
    def __init__(self, connectors: dict | None = None):
        self.mirror_channel_calls: list[dict] = []
        self.mirror_channel_all_calls: list[dict] = []
        self.link_channel_calls: list[dict] = []
        self.list_linked_channels_calls: list[dict] = []
        self.link_user_calls: list[dict] = []
        self.list_linked_users_calls: list[dict] = []
        self.unlink_channel_calls: list[dict] = []
        self.unlink_user_calls: list[dict] = []
        self.connectors = connectors or {}

    async def link_user(self, **kwargs):
        self.link_user_calls.append(kwargs)
        return "user linked ok"

    async def list_linked_users(self, **kwargs):
        self.list_linked_users_calls.append(kwargs)
        return "Linked users:\nDiscord: ShrinerH (216591124222050304) ↔ Stoat: shriner (01KH)"

    async def mirror_channel(self, **kwargs):
        self.mirror_channel_calls.append(kwargs)
        return "ok"

    async def mirror_channel_all(self, **kwargs):
        self.mirror_channel_all_calls.append(kwargs)
        return "ok"

    async def link_channel(self, **kwargs):
        self.link_channel_calls.append(kwargs)
        return "ok"

    async def list_linked_channels(self, **kwargs):
        self.list_linked_channels_calls.append(kwargs)
        return "Linked channels:\nDiscord: general (999) (this channel)"

    async def unlink_channel(self, **kwargs):
        self.unlink_channel_calls.append(kwargs)
        return "unlinked ok"

    async def unlink_user(self, **kwargs):
        self.unlink_user_calls.append(kwargs)
        return "user unlinked ok"


class FakeCategoryLinker:
    def __init__(self, connectors: dict | None = None):
        self.link_category_calls: list[dict] = []
        self.list_linked_categories_calls: list[dict] = []
        self.unlink_category_calls: list[dict] = []
        self.sync_new_channel_calls: list[dict] = []
        self.mirror_category_calls: list[dict] = []
        self.mirror_category_all_calls: list[dict] = []
        self.connectors = connectors or {}

    async def link_category(self, **kwargs):
        self.link_category_calls.append(kwargs)
        return "category linked ok"

    async def list_linked_categories(self, **kwargs):
        self.list_linked_categories_calls.append(kwargs)
        return "Linked categories:\nDiscord: Team (999) (this Category)"

    async def unlink_category(self, **kwargs):
        self.unlink_category_calls.append(kwargs)
        return "category unlinked ok"

    async def sync_new_channel(self, **kwargs):
        self.sync_new_channel_calls.append(kwargs)

    async def mirror_category(self, **kwargs):
        self.mirror_category_calls.append(kwargs)
        return "mirrored ok"

    async def mirror_category_all(self, **kwargs):
        self.mirror_category_all_calls.append(kwargs)
        return "mirrored all ok"


class FakeInteraction:
    def __init__(
        self,
        channel_id: int = 999,
        channel_name: str = "current-channel",
        user_id: int = 1,
        category: SimpleNamespace | None = None,
    ):
        self.channel_id = channel_id
        self.channel = SimpleNamespace(name=channel_name, category=category)
        self.user = SimpleNamespace(id=user_id)
        self.sent: list[str] = []
        self.response = SimpleNamespace(send_message=self._send_message, defer=self._defer)
        self.followup = SimpleNamespace(send=self._send_message)
        self.deferred = False

    async def _send_message(self, content, ephemeral=False):
        self.sent.append(content)

    async def _defer(self, ephemeral=False, thinking=False):
        self.deferred = True


def _make_sender(
    linker: FakeLinker, *, emote_linker=None, user_linker=None, category_linker=None, role_linker=None
) -> DiscordSenderService:
    return DiscordSenderService(
        _discord_config(),
        on_message=_noop,
        health=HealthTracker({"discord": "Discord"}),
        linker=linker,
        emote_linker=emote_linker,
        user_linker=user_linker,
        category_linker=category_linker,
        role_linker=role_linker,
    )


# ---------------------------------------------------------------- _normalize_channel_id


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("<#814279082606592020>", "814279082606592020"),
        ("814279082606592020", "814279082606592020"),
        ("general", "general"),  # a Stoat/IRC-style id must pass through untouched
        (" <#123> ", "123"),  # tolerate incidental whitespace
    ],
)
def test_normalize_channel_id(raw, expected):
    assert _normalize_channel_id(raw) == expected


# ---------------------------------------------------------------- _handle_mirror_channel


async def test_mirror_channel_strips_a_pasted_mention_and_resolves_its_real_name(monkeypatch):
    linker = FakeLinker()
    sender = _make_sender(linker)

    async def fake_get_channel_name(channel_id: str):
        assert channel_id == "814279082606592020"
        return "general"

    monkeypatch.setattr(sender, "get_channel_name", fake_get_channel_name)
    interaction = FakeInteraction()

    await sender._handle_mirror_channel(interaction, "stoat", "<#814279082606592020>")

    assert len(linker.mirror_channel_calls) == 1
    call = linker.mirror_channel_calls[0]
    assert call["local_channel_id"] == "814279082606592020"
    assert call["local_channel_name"] == "general"


async def test_mirror_channel_falls_back_to_bare_id_when_name_unresolvable(monkeypatch):
    linker = FakeLinker()
    sender = _make_sender(linker)

    async def fake_get_channel_name(_channel_id: str):
        return None  # e.g. the channel isn't in this guild's cache

    monkeypatch.setattr(sender, "get_channel_name", fake_get_channel_name)
    interaction = FakeInteraction()

    await sender._handle_mirror_channel(interaction, "all", "<#42>")

    call = linker.mirror_channel_all_calls[0]
    assert call["local_channel_id"] == "42"
    assert call["local_channel_name"] == "42"


async def test_mirror_channel_uses_invoking_channel_when_no_explicit_id_given():
    linker = FakeLinker()
    sender = _make_sender(linker)
    interaction = FakeInteraction(channel_id=555, channel_name="the-current-one")

    await sender._handle_mirror_channel(interaction, "stoat", None)

    call = linker.mirror_channel_calls[0]
    assert call["local_channel_id"] == "555"
    assert call["local_channel_name"] == "the-current-one"


# ---------------------------------------------------------------- _handle_link_channel


async def test_link_channel_normalizes_a_pasted_mention_in_destination_id():
    linker = FakeLinker()
    sender = _make_sender(linker)
    interaction = FakeInteraction()

    await sender._handle_link_channel(interaction, "stoat", "s1", "<#814279082606592020>")

    call = linker.link_channel_calls[0]
    assert call["destination_id"] == "814279082606592020"


async def test_link_channel_normalizes_a_pasted_mention_in_source_id():
    linker = FakeLinker()
    sender = _make_sender(linker)
    interaction = FakeInteraction()

    await sender._handle_link_channel(interaction, "discord", "<#111>", None)

    call = linker.link_channel_calls[0]
    assert call["source_id"] == "111"


async def test_link_channel_leaves_a_missing_destination_id_as_none():
    linker = FakeLinker()
    sender = _make_sender(linker)
    interaction = FakeInteraction()

    await sender._handle_link_channel(interaction, "stoat", "s1", None)

    call = linker.link_channel_calls[0]
    assert call["destination_id"] is None


# ---------------------------------------------------------------- _handle_linked_channels


async def test_linked_channels_reports_the_invoking_channel():
    linker = FakeLinker()
    sender = _make_sender(linker)
    interaction = FakeInteraction(channel_id=555)

    await sender._handle_linked_channels(interaction)

    assert linker.list_linked_channels_calls == [{"local_connector": "discord", "local_channel_id": "555"}]
    assert interaction.sent == ["Linked channels:\nDiscord: general (999) (this channel)"]


async def test_linked_channels_without_a_configured_linker():
    sender = DiscordSenderService(
        _discord_config(), on_message=_noop, health=HealthTracker({"discord": "Discord"}), linker=None
    )
    interaction = FakeInteraction()

    await sender._handle_linked_channels(interaction)

    assert interaction.sent == ["Linking isn't configured."]


# ---------------------------------------------------------------- _handle_link_user


async def test_link_user_uses_the_picked_members_id_not_free_text():
    # local_user is a discord.Member (a real search/pick from Discord's own
    # UI), not a typed string - this is the fix for a real bug: a plain
    # string option let someone type "@shrinerh" instead of the actual
    # snowflake, which silently linked the wrong (nonexistent) id and broke
    # mention rewriting in both directions.
    user_linker = FakeLinker()
    sender = _make_sender(FakeLinker(), user_linker=user_linker)
    interaction = FakeInteraction()
    member = SimpleNamespace(id=216591124222050304)

    await sender._handle_link_user(interaction, "stoat", "01KH7TH31EBY08FTQ7YC2RC4DQ", member)

    assert user_linker.link_user_calls == [
        {
            "local_connector": "discord",
            "local_user_id": "216591124222050304",
            "source": "stoat",
            "source_user_id": "01KH7TH31EBY08FTQ7YC2RC4DQ",
        }
    ]
    assert interaction.sent == ["user linked ok"]


async def test_link_user_without_a_configured_user_linker():
    sender = _make_sender(FakeLinker(), user_linker=None)
    interaction = FakeInteraction()

    await sender._handle_link_user(interaction, "stoat", "01KH", SimpleNamespace(id=111))

    assert interaction.sent == ["User linking isn't configured."]


# ---------------------------------------------------------------- _handle_linked_users


async def test_linked_users_with_a_member_shows_only_their_link():
    user_linker = FakeLinker()
    sender = _make_sender(FakeLinker(), user_linker=user_linker)
    interaction = FakeInteraction()
    member = SimpleNamespace(id=216591124222050304)

    await sender._handle_linked_users(interaction, member)

    assert user_linker.list_linked_users_calls == [
        {"local_connector": "discord", "local_user_id": "216591124222050304"}
    ]
    assert interaction.sent == ["Linked users:\nDiscord: ShrinerH (216591124222050304) ↔ Stoat: shriner (01KH)"]


async def test_linked_users_with_no_member_lists_everything():
    user_linker = FakeLinker()
    sender = _make_sender(FakeLinker(), user_linker=user_linker)
    interaction = FakeInteraction()

    await sender._handle_linked_users(interaction, None)

    assert user_linker.list_linked_users_calls == [{}]


async def test_linked_users_without_a_configured_user_linker():
    sender = _make_sender(FakeLinker(), user_linker=None)
    interaction = FakeInteraction()

    await sender._handle_linked_users(interaction, None)

    assert interaction.sent == ["User linking isn't configured."]


# ---------------------------------------------------------------- _handle_unlink_channel


async def test_unlink_channel_defaults_destination_to_none():
    linker = FakeLinker()
    sender = _make_sender(linker)
    interaction = FakeInteraction(channel_id=999)

    await sender._handle_unlink_channel(interaction, None, None)

    assert linker.unlink_channel_calls == [{"local_connector": "discord", "local_channel_id": "999", "destination": None}]
    assert interaction.sent == ["unlinked ok"]


async def test_unlink_channel_with_a_specific_destination():
    linker = FakeLinker()
    sender = _make_sender(linker)
    interaction = FakeInteraction(channel_id=999)

    await sender._handle_unlink_channel(interaction, "stoat", None)

    assert linker.unlink_channel_calls == [{"local_connector": "discord", "local_channel_id": "999", "destination": "stoat"}]


async def test_unlink_channel_with_a_specific_local_channel_id():
    linker = FakeLinker()
    sender = _make_sender(linker)
    interaction = FakeInteraction(channel_id=999)

    await sender._handle_unlink_channel(interaction, "stoat", "555")

    assert linker.unlink_channel_calls == [{"local_connector": "discord", "local_channel_id": "555", "destination": "stoat"}]


async def test_unlink_channel_without_a_configured_linker():
    sender = _make_sender(None)
    interaction = FakeInteraction()

    await sender._handle_unlink_channel(interaction, None, None)

    assert interaction.sent == ["Linking isn't configured."]


# ---------------------------------------------------------------- _handle_unlink_user


async def test_unlink_user_defaults_to_the_invoking_member():
    user_linker = FakeLinker()
    sender = _make_sender(FakeLinker(), user_linker=user_linker)
    interaction = FakeInteraction(user_id=111)

    await sender._handle_unlink_user(interaction, None, None)

    assert user_linker.unlink_user_calls == [{"local_connector": "discord", "local_user_id": "111", "destination": None}]
    assert interaction.sent == ["user unlinked ok"]


async def test_unlink_user_with_a_specific_destination_and_target():
    user_linker = FakeLinker()
    sender = _make_sender(FakeLinker(), user_linker=user_linker)
    interaction = FakeInteraction(user_id=111)
    member = SimpleNamespace(id=222)

    await sender._handle_unlink_user(interaction, "stoat", member)

    assert user_linker.unlink_user_calls == [{"local_connector": "discord", "local_user_id": "222", "destination": "stoat"}]


async def test_unlink_user_without_a_configured_user_linker():
    sender = _make_sender(FakeLinker(), user_linker=None)
    interaction = FakeInteraction()

    await sender._handle_unlink_user(interaction, None, None)

    assert interaction.sent == ["User linking isn't configured."]


@pytest.fixture
def sample_connectors():
    async def _resolve(_id):
        return None

    # Discord/Stoat wire the role/Category/emoji resolve hooks; IRC (which has
    # none of those concepts) wires none - so ConnectorInfo.supports_roles /
    # supports_categories / supports_emotes are True for the first two and
    # False for IRC, matching bridge.run()'s real wiring.
    _rce = dict(resolve_role_name=_resolve, resolve_category_name=_resolve, resolve_emoji_name=_resolve)
    return {
        "discord": ConnectorInfo(id="discord", label="Discord", **_rce),
        "stoat-public": ConnectorInfo(id="stoat-public", label="Stoat (public)", **_rce),
        "irc": ConnectorInfo(id="irc", label="IRC"),
    }


def test_autocomplete_choices_lists_everything_for_an_empty_query(sample_connectors):
    choices = _connector_autocomplete_choices("", sample_connectors)
    assert {c.value for c in choices} == {"discord", "stoat-public", "irc"}


def test_autocomplete_choices_filters_by_connector_id_substring(sample_connectors):
    choices = _connector_autocomplete_choices("stoat", sample_connectors)
    assert [c.value for c in choices] == ["stoat-public"]


def test_autocomplete_choices_filters_by_label_substring_case_insensitively(sample_connectors):
    choices = _connector_autocomplete_choices("PUBLIC", sample_connectors)
    assert [c.value for c in choices] == ["stoat-public"]


def test_autocomplete_choices_no_match_returns_empty(sample_connectors):
    assert _connector_autocomplete_choices("webchat", sample_connectors) == []


def test_autocomplete_choices_empty_connectors_returns_empty():
    assert _connector_autocomplete_choices("anything", {}) == []


def test_autocomplete_choices_caps_at_25():
    many = {f"c{i}": ConnectorInfo(id=f"c{i}", label=f"Connector {i}") for i in range(30)}
    assert len(_connector_autocomplete_choices("", many)) == 25


def test_autocomplete_choices_include_all_adds_the_all_choice_first(sample_connectors):
    choices = _connector_autocomplete_choices("", sample_connectors, include_all=True)
    assert choices[0].value == "all"
    assert {c.value for c in choices} == {"all", "discord", "stoat-public", "irc"}


def test_autocomplete_choices_include_all_respects_the_filter(sample_connectors):
    choices = _connector_autocomplete_choices("disc", sample_connectors, include_all=True)
    assert [c.value for c in choices] == ["discord"]  # "all" doesn't match "disc" - excluded


def test_autocomplete_choices_all_not_added_when_include_all_is_false(sample_connectors):
    choices = _connector_autocomplete_choices("", sample_connectors, include_all=False)
    assert "all" not in {c.value for c in choices}


# ---------------------------------------------------------------- autocomplete wiring on the slash commands


def _autocomplete_callback(sender: DiscordSenderService, command_name: str, param_name: str):
    # `command_name` is either a flat command name ("link-emote") or a
    # "group sub" pair ("link channel") for an app_commands.Group subcommand.
    parts = command_name.split()
    command = sender.tree.get_command(parts[0], guild=sender._guild)
    for sub in parts[1:]:
        command = command.get_command(sub)
    return command._params[param_name].autocomplete


def test_user_commands_are_registered_as_subcommands_not_flat(sample_connectors):
    sender = _make_sender(FakeLinker())
    for group, sub in (("link", "user"), ("unlink", "user"), ("linked", "users")):
        node = sender.tree.get_command(group, guild=sender._guild)
        assert node is not None and node.get_command(sub) is not None
    # the old flat names are gone
    for flat in ("link-user", "unlink-user", "linked-users"):
        assert sender.tree.get_command(flat, guild=sender._guild) is None


def test_emote_commands_are_registered_as_subcommands_not_flat(sample_connectors):
    sender = _make_sender(FakeLinker())
    for group, sub in (("link", "emote"), ("unlink", "emote"), ("linked", "emotes"), ("mirror", "emote")):
        node = sender.tree.get_command(group, guild=sender._guild)
        assert node is not None and node.get_command(sub) is not None
    assert sender.tree.get_command("link-emote", guild=sender._guild) is None


async def test_link_channel_source_autocomplete_is_wired_to_the_linker(sample_connectors):
    sender = _make_sender(FakeLinker(sample_connectors))
    callback = _autocomplete_callback(sender, "link channel", "service")

    choices = await callback(FakeInteraction(), "stoat")

    assert [c.value for c in choices] == ["stoat-public"]


async def test_link_category_source_autocomplete_is_wired_to_the_category_linker(sample_connectors):
    sender = _make_sender(FakeLinker(), category_linker=FakeCategoryLinker(sample_connectors))
    callback = _autocomplete_callback(sender, "link category", "service")

    choices = await callback(FakeInteraction(), "stoat")

    assert [c.value for c in choices] == ["stoat-public"]


# ---------------------------------------------------------------- _handle_linked_categories


async def test_linked_categories_reports_the_invoking_categorys_id():
    category_linker = FakeCategoryLinker()
    sender = _make_sender(FakeLinker(), category_linker=category_linker)
    interaction = FakeInteraction(category=SimpleNamespace(id=777, name="Team"))

    await sender._handle_linked_categories(interaction)

    assert category_linker.list_linked_categories_calls == [
        {"local_connector": "discord", "local_category_id": "777", "local_category": None}
    ]
    assert interaction.sent == ["Linked categories:\nDiscord: Team (999) (this Category)"]


async def test_linked_categories_without_a_configured_category_linker():
    sender = _make_sender(FakeLinker(), category_linker=None)
    interaction = FakeInteraction(category=SimpleNamespace(id=777, name="Team"))

    await sender._handle_linked_categories(interaction)

    assert interaction.sent == ["Category linking isn't configured."]


async def test_linked_categories_when_invoking_channel_has_no_category():
    category_linker = FakeCategoryLinker()
    sender = _make_sender(FakeLinker(), category_linker=category_linker)
    interaction = FakeInteraction(category=None)

    await sender._handle_linked_categories(interaction)

    assert interaction.sent == ["This channel isn't inside a Category."]
    assert category_linker.list_linked_categories_calls == []


# ---------------------------------------------------------------- _handle_link_category


async def test_link_category_uses_the_invoking_channels_category_id_and_name():
    category_linker = FakeCategoryLinker()
    sender = _make_sender(FakeLinker(), category_linker=category_linker)
    interaction = FakeInteraction(category=SimpleNamespace(id=777, name="Team"))

    await sender._handle_link_category(interaction, "stoat", "s-cat", None)

    assert category_linker.link_category_calls == [
        {
            "local_connector": "discord",
            "local_category_id": "777",
            "local_category_name": "Team",
            "source": "stoat",
            "source_id": "s-cat",
            "destination_id": None,
        }
    ]
    assert interaction.sent == ["category linked ok"]


async def test_link_category_normalizes_a_pasted_mention_in_source_and_destination_id():
    category_linker = FakeCategoryLinker()
    sender = _make_sender(FakeLinker(), category_linker=category_linker)
    interaction = FakeInteraction(category=SimpleNamespace(id=777, name="Team"))

    await sender._handle_link_category(interaction, "discord", "<#111>", "<#222>")

    call = category_linker.link_category_calls[0]
    assert call["source_id"] == "111"
    assert call["destination_id"] == "222"


async def test_link_category_without_a_configured_category_linker():
    sender = _make_sender(FakeLinker(), category_linker=None)
    interaction = FakeInteraction(category=SimpleNamespace(id=777, name="Team"))

    await sender._handle_link_category(interaction, "stoat", "s-cat", None)

    assert interaction.sent == ["Category linking isn't configured."]


async def test_link_category_when_invoking_channel_has_no_category():
    category_linker = FakeCategoryLinker()
    sender = _make_sender(FakeLinker(), category_linker=category_linker)
    interaction = FakeInteraction(category=None)

    await sender._handle_link_category(interaction, "stoat", "s-cat", None)

    assert interaction.sent == ["This channel isn't inside a Category."]
    assert category_linker.link_category_calls == []


async def test_link_category_reports_a_link_error_instead_of_raising():
    class RejectingCategoryLinker(FakeCategoryLinker):
        async def link_category(self, **kwargs):
            from stoat_discord_bridge.admin_commands import LinkError

            raise LinkError("that Category is used for thread mirroring and can't be linked")

    sender = _make_sender(FakeLinker(), category_linker=RejectingCategoryLinker())
    interaction = FakeInteraction(category=SimpleNamespace(id=777, name="Threads"))

    await sender._handle_link_category(interaction, "stoat", "s-cat", None)

    assert interaction.sent == ["that Category is used for thread mirroring and can't be linked"]


# ---------------------------------------------------------------- _handle_unlink_category


async def test_unlink_category_defaults_destination_to_none():
    category_linker = FakeCategoryLinker()
    sender = _make_sender(FakeLinker(), category_linker=category_linker)
    interaction = FakeInteraction(category=SimpleNamespace(id=777, name="Team"))

    await sender._handle_unlink_category(interaction, None)

    assert category_linker.unlink_category_calls == [
        {"local_connector": "discord", "local_category_id": "777", "local_category": None, "destination": None}
    ]
    assert interaction.sent == ["category unlinked ok"]


async def test_unlink_category_with_a_specific_destination():
    category_linker = FakeCategoryLinker()
    sender = _make_sender(FakeLinker(), category_linker=category_linker)
    interaction = FakeInteraction(category=SimpleNamespace(id=777, name="Team"))

    await sender._handle_unlink_category(interaction, None, "stoat")

    assert category_linker.unlink_category_calls == [
        {"local_connector": "discord", "local_category_id": "777", "local_category": None, "destination": "stoat"}
    ]


async def test_unlink_category_without_a_configured_category_linker():
    sender = _make_sender(FakeLinker(), category_linker=None)
    interaction = FakeInteraction(category=SimpleNamespace(id=777, name="Team"))

    await sender._handle_unlink_category(interaction, None)

    assert interaction.sent == ["Category linking isn't configured."]


async def test_unlink_category_when_invoking_channel_has_no_category():
    category_linker = FakeCategoryLinker()
    sender = _make_sender(FakeLinker(), category_linker=category_linker)
    interaction = FakeInteraction(category=None)

    await sender._handle_unlink_category(interaction, None)

    assert interaction.sent == ["This channel isn't inside a Category."]
    assert category_linker.unlink_category_calls == []


async def test_unlink_category_reports_a_link_error_instead_of_raising():
    class RejectingCategoryLinker(FakeCategoryLinker):
        async def unlink_category(self, **kwargs):
            from stoat_discord_bridge.admin_commands import LinkError

            raise LinkError("this Category isn't linked")

    sender = _make_sender(FakeLinker(), category_linker=RejectingCategoryLinker())
    interaction = FakeInteraction(category=SimpleNamespace(id=777, name="Team"))

    await sender._handle_unlink_category(interaction, None)

    assert interaction.sent == ["this Category isn't linked"]


# ---------------------------------------------------------------- _handle_mirror_category


async def test_mirror_category_all_dispatches_to_mirror_category_all():
    category_linker = FakeCategoryLinker()
    sender = _make_sender(FakeLinker(), category_linker=category_linker)
    interaction = FakeInteraction(category=SimpleNamespace(id=777, name="Team"))

    await sender._handle_mirror_category(interaction, None, None)

    assert category_linker.mirror_category_all_calls == [
        {"local_connector": "discord", "local_category_id": "777", "local_category": None}
    ]
    assert interaction.sent == ["mirrored all ok"]


async def test_mirror_category_to_a_named_local_category_and_one_destination():
    category_linker = FakeCategoryLinker()
    sender = _make_sender(FakeLinker(), category_linker=category_linker)
    interaction = FakeInteraction(category=None)

    await sender._handle_mirror_category(interaction, "Team Chat", "stoat")

    assert category_linker.mirror_category_calls == [
        {"local_connector": "discord", "local_category_id": None, "local_category": "Team Chat", "destination": "stoat"}
    ]
    assert interaction.sent == ["mirrored ok"]


async def test_mirror_category_without_a_category_or_token_errors():
    category_linker = FakeCategoryLinker()
    sender = _make_sender(FakeLinker(), category_linker=category_linker)
    interaction = FakeInteraction(category=None)

    await sender._handle_mirror_category(interaction, None, None)

    assert interaction.sent == ["This channel isn't inside a Category."]
    assert category_linker.mirror_category_all_calls == []


# ---------------------------------------------------------------- category-name hooks


async def test_resolve_category_id_by_name_matches_by_name_and_passes_ids_through(monkeypatch):
    sender = _make_sender(FakeLinker())
    guild = SimpleNamespace(
        categories=[SimpleNamespace(id=10, name="Team Chat"), SimpleNamespace(id=20, name="Ops")]
    )
    monkeypatch.setattr(sender, "_guild_or_none", lambda: guild)

    assert await sender.resolve_category_id_by_name("team chat") == "10"
    assert await sender.resolve_category_id_by_name("10") == "10"
    assert await sender.resolve_category_id_by_name("missing") is None


# ---------------------------------------------------------------- _handle_channel_create


async def test_handle_channel_create_syncs_a_new_channel_in_a_linked_category():
    category_linker = FakeCategoryLinker()
    sender = _make_sender(FakeLinker(), category_linker=category_linker)
    guild = FakeGuild(id=123)
    category = FakeGuildChannel(id=555, name="Team", guild=guild)
    channel = FakeGuildChannel(id=888, name="general-2", guild=guild, category=category)

    await sender._handle_channel_create(channel)

    assert category_linker.sync_new_channel_calls == [
        {
            "local_connector": "discord",
            "local_category_id": "555",
            "channel_id": "888",
            "channel_name": "general-2",
        }
    ]


async def test_handle_channel_create_noop_without_a_configured_category_linker():
    sender = _make_sender(FakeLinker(), category_linker=None)
    guild = FakeGuild(id=123)
    category = FakeGuildChannel(id=555, name="Team", guild=guild)
    channel = FakeGuildChannel(id=888, name="general-2", guild=guild, category=category)

    await sender._handle_channel_create(channel)  # would raise if it tried to use a None category_linker


async def test_handle_channel_create_noop_for_a_channel_outside_the_configured_guild():
    category_linker = FakeCategoryLinker()
    sender = _make_sender(FakeLinker(), category_linker=category_linker)
    guild = FakeGuild(id=999)  # not this sender's configured guild_id (123)
    category = FakeGuildChannel(id=555, name="Team", guild=guild)
    channel = FakeGuildChannel(id=888, name="general-2", guild=guild, category=category)

    await sender._handle_channel_create(channel)

    assert category_linker.sync_new_channel_calls == []


async def test_handle_channel_create_noop_for_a_channel_with_no_category():
    category_linker = FakeCategoryLinker()
    sender = _make_sender(FakeLinker(), category_linker=category_linker)
    guild = FakeGuild(id=123)
    channel = FakeGuildChannel(id=888, name="general-2", guild=guild, category=None)

    await sender._handle_channel_create(channel)

    assert category_linker.sync_new_channel_calls == []


async def test_handle_channel_create_noop_for_a_non_text_or_voice_channel():
    category_linker = FakeCategoryLinker()
    sender = _make_sender(FakeLinker(), category_linker=category_linker)
    guild = FakeGuild(id=123)
    category = FakeGuildChannel(id=555, name="Team", guild=guild)
    not_a_channel = SimpleNamespace(id=888, name="whatever", guild=guild, category=category)

    await sender._handle_channel_create(not_a_channel)

    assert category_linker.sync_new_channel_calls == []


async def test_link_channel_source_autocomplete_handles_no_configured_linker():
    sender = DiscordSenderService(
        _discord_config(), on_message=_noop, health=HealthTracker({"discord": "Discord"}), linker=None
    )
    callback = _autocomplete_callback(sender, "link channel", "service")

    assert await callback(FakeInteraction(), "") == []


async def test_link_emote_source_autocomplete_reads_the_emote_linker(sample_connectors):
    sender = _make_sender(FakeLinker(), emote_linker=FakeLinker(sample_connectors))
    callback = _autocomplete_callback(sender, "link emote", "service")

    choices = await callback(FakeInteraction(), "stoat")

    assert [c.value for c in choices] == ["stoat-public"]


# ---------------------------------------------------------------- issue #26: role/Category/emote `service`
# autocomplete must not offer IRC (no such concept there)


async def test_link_role_service_autocomplete_hides_irc(sample_connectors):
    sender = _make_sender(FakeLinker(), role_linker=FakeLinker(sample_connectors))
    callback = _autocomplete_callback(sender, "link role", "service")

    choices = await callback(FakeInteraction(), "")

    assert {c.value for c in choices} == {"discord", "stoat-public"}
    assert await callback(FakeInteraction(), "irc") == []


async def test_link_category_service_autocomplete_hides_irc(sample_connectors):
    sender = _make_sender(FakeLinker(), category_linker=FakeCategoryLinker(sample_connectors))
    callback = _autocomplete_callback(sender, "link category", "service")

    choices = await callback(FakeInteraction(), "")

    assert {c.value for c in choices} == {"discord", "stoat-public"}


async def test_link_emote_service_autocomplete_hides_irc(sample_connectors):
    sender = _make_sender(FakeLinker(), emote_linker=FakeLinker(sample_connectors))
    callback = _autocomplete_callback(sender, "link emote", "service")

    choices = await callback(FakeInteraction(), "")

    assert {c.value for c in choices} == {"discord", "stoat-public"}


async def test_mirror_role_service_autocomplete_hides_irc_but_keeps_all(sample_connectors):
    sender = _make_sender(FakeLinker(), role_linker=FakeLinker(sample_connectors))
    callback = _autocomplete_callback(sender, "mirror role", "service")

    choices = await callback(FakeInteraction(), "")

    assert {c.value for c in choices} == {"all", "discord", "stoat-public"}


async def test_link_channel_service_autocomplete_still_offers_irc(sample_connectors):
    # channels exist on every connector kind - IRC must stay in the list here
    sender = _make_sender(FakeLinker(sample_connectors))
    callback = _autocomplete_callback(sender, "link channel", "service")

    choices = await callback(FakeInteraction(), "")

    assert "irc" in {c.value for c in choices}


async def test_link_user_source_autocomplete_reads_the_user_linker(sample_connectors):
    sender = _make_sender(FakeLinker(), user_linker=FakeLinker(sample_connectors))
    callback = _autocomplete_callback(sender, "link user", "service")

    choices = await callback(FakeInteraction(), "irc")

    assert [c.value for c in choices] == ["irc"]


async def test_mirror_channel_destination_autocomplete_includes_all(sample_connectors):
    sender = _make_sender(FakeLinker(sample_connectors))
    callback = _autocomplete_callback(sender, "mirror channel", "service")

    choices = await callback(FakeInteraction(), "")

    assert {c.value for c in choices} == {"all", "discord", "stoat-public", "irc"}
