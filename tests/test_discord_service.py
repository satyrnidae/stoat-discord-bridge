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


class FakeInteraction:
    def __init__(self, channel_id: int = 999, channel_name: str = "current-channel", user_id: int = 1):
        self.channel_id = channel_id
        self.channel = SimpleNamespace(name=channel_name)
        self.user = SimpleNamespace(id=user_id)
        self.sent: list[str] = []
        self.response = SimpleNamespace(send_message=self._send_message)

    async def _send_message(self, content, ephemeral=False):
        self.sent.append(content)


def _make_sender(linker: FakeLinker, *, emote_linker=None, user_linker=None) -> DiscordSenderService:
    return DiscordSenderService(
        _discord_config(),
        on_message=_noop,
        health=HealthTracker({"discord": "Discord"}),
        linker=linker,
        emote_linker=emote_linker,
        user_linker=user_linker,
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
    return {
        "discord": ConnectorInfo(id="discord", label="Discord"),
        "stoat-public": ConnectorInfo(id="stoat-public", label="Stoat (public)"),
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
    command = sender.tree.get_command(command_name, guild=sender._guild)
    return command._params[param_name].autocomplete


async def test_link_channel_source_autocomplete_is_wired_to_the_linker(sample_connectors):
    sender = _make_sender(FakeLinker(sample_connectors))
    callback = _autocomplete_callback(sender, "link-channel", "source")

    choices = await callback(FakeInteraction(), "stoat")

    assert [c.value for c in choices] == ["stoat-public"]


async def test_link_channel_source_autocomplete_handles_no_configured_linker():
    sender = DiscordSenderService(
        _discord_config(), on_message=_noop, health=HealthTracker({"discord": "Discord"}), linker=None
    )
    callback = _autocomplete_callback(sender, "link-channel", "source")

    assert await callback(FakeInteraction(), "") == []


async def test_link_emote_source_autocomplete_reads_the_emote_linker(sample_connectors):
    sender = _make_sender(FakeLinker(), emote_linker=FakeLinker(sample_connectors))
    callback = _autocomplete_callback(sender, "link-emote", "source")

    choices = await callback(FakeInteraction(), "irc")

    assert [c.value for c in choices] == ["irc"]


async def test_link_user_source_autocomplete_reads_the_user_linker(sample_connectors):
    sender = _make_sender(FakeLinker(), user_linker=FakeLinker(sample_connectors))
    callback = _autocomplete_callback(sender, "link-user", "source")

    choices = await callback(FakeInteraction(), "irc")

    assert [c.value for c in choices] == ["irc"]


async def test_mirror_channel_destination_autocomplete_includes_all(sample_connectors):
    sender = _make_sender(FakeLinker(sample_connectors))
    callback = _autocomplete_callback(sender, "mirror-channel", "destination")

    choices = await callback(FakeInteraction(), "")

    assert {c.value for c in choices} == {"all", "discord", "stoat-public", "irc"}
