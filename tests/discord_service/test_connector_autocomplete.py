from __future__ import annotations

import pytest

from stoat_discord_bridge.admin_commands import ConnectorInfo
from stoat_discord_bridge.services.discord_service import DiscordSenderService, _connector_autocomplete_choices
from stoat_discord_bridge.status import HealthTracker
from tests.discord_service.conftest import (
    FakeCategoryLinker,
    FakeInteraction,
    FakeLinker,
    _autocomplete_callback,
    _discord_config,
    _make_sender,
    _noop,
)


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
    callback = _autocomplete_callback(sender, "mirror role to", "service")

    choices = await callback(FakeInteraction(), "")

    assert {c.value for c in choices} == {"all", "discord", "stoat-public"}


async def test_mirror_role_from_service_autocomplete_hides_irc_and_all(sample_connectors):
    sender = _make_sender(FakeLinker(), role_linker=FakeLinker(sample_connectors))
    callback = _autocomplete_callback(sender, "mirror role from", "service")

    choices = await callback(FakeInteraction(), "")

    assert {c.value for c in choices} == {"discord", "stoat-public"}


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
    callback = _autocomplete_callback(sender, "mirror channel to", "service")

    choices = await callback(FakeInteraction(), "")

    assert {c.value for c in choices} == {"all", "discord", "stoat-public", "irc"}


async def test_mirror_channel_from_source_autocomplete_excludes_all(sample_connectors):
    sender = _make_sender(FakeLinker(sample_connectors))
    callback = _autocomplete_callback(sender, "mirror channel from", "service")

    choices = await callback(FakeInteraction(), "")

    assert {c.value for c in choices} == {"discord", "stoat-public", "irc"}


