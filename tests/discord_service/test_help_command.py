"""Discord's `/help [topic] [noun]` command (issues #117, #172) -
registration, the no-arg index, topic drill-down, and the `noun`
autocomplete. Renders via the shared `admin_commands.help` table every
connector's help command draws from.
"""

from __future__ import annotations

from types import SimpleNamespace

from discord import app_commands

from stoat_discord_bridge.admin_commands import HELP_TOPICS, render_help
from stoat_discord_bridge.services.discord_service.commands import (
    _CHOICE_LIMIT,
    _help_noun_choices,
    _help_topic_choices,
)
from tests.discord_service.conftest import (
    FakeInteraction,
    FakeLinker,
    _autocomplete_callback,
    _make_sender,
)


def _help_command(sender):
    return sender.tree.get_command("help", guild=sender._guild)


def _verbs(topics) -> set[str]:
    return {key.split(" ", 1)[0] for key in topics}


async def test_help_command_topic_choices_are_the_distinct_verbs():
    sender = _make_sender(FakeLinker())
    command = _help_command(sender)

    assert command is not None
    [topic_param] = [p for p in command.parameters if p.name == "topic"]
    assert {choice.value for choice in topic_param.choices} == _verbs(HELP_TOPICS)
    assert len(topic_param.choices) <= _CHOICE_LIMIT


def test_topic_choices_stay_under_the_cap_as_help_topics_grows():
    # Well past Discord's 25-choice cap, but only a few distinct verbs.
    topics = dict(HELP_TOPICS)
    for i in range(40):
        topics[f"link thing{i}"] = HELP_TOPICS["link channel"]

    choices = _help_topic_choices(topics)

    assert len(HELP_TOPICS) < len(topics)
    assert {choice.value for choice in choices} == _verbs(topics)
    assert len(choices) <= _CHOICE_LIMIT


def test_noun_choices_for_a_verb_list_its_nouns():
    choices = _help_noun_choices("link", "")

    assert [choice.value for choice in choices] == ["channel", "user", "role", "category", "emote"]


def test_noun_choices_filter_by_current_text():
    assert [choice.value for choice in _help_noun_choices("linked", "CAT")] == ["categories"]


def test_noun_choices_empty_for_a_bare_verb_or_no_topic():
    assert _help_noun_choices("status", "") == []
    assert _help_noun_choices(None, "") == []


def test_noun_choices_capped_at_the_choice_limit():
    topics = {f"link thing{i}": HELP_TOPICS["link channel"] for i in range(40)}

    assert len(_help_noun_choices("link", "", topics)) == _CHOICE_LIMIT


async def test_noun_autocomplete_reads_the_chosen_topic():
    sender = _make_sender(FakeLinker())
    autocomplete = _autocomplete_callback(sender, "help", "noun")
    interaction = FakeInteraction(namespace=SimpleNamespace(topic="mirror"))

    choices = await autocomplete(interaction, "")

    assert [choice.value for choice in choices] == ["channel", "role", "category", "emote"]


async def test_help_command_with_no_topic_sends_the_index():
    sender = _make_sender(FakeLinker())
    interaction = FakeInteraction()

    await _help_command(sender).callback(interaction, topic=None)

    assert interaction.sent == [render_help(None, connector="discord")]


async def test_help_command_with_a_topic_and_noun_drills_down():
    sender = _make_sender(FakeLinker())
    interaction = FakeInteraction()

    await _help_command(sender).callback(
        interaction, topic=app_commands.Choice(name="x", value="link"), noun="channel"
    )

    assert interaction.sent == [render_help("link channel", connector="discord")]


async def test_help_command_with_a_bare_verb_topic_drills_down():
    sender = _make_sender(FakeLinker())
    interaction = FakeInteraction()

    await _help_command(sender).callback(interaction, topic=app_commands.Choice(name="x", value="status"))

    assert interaction.sent == [render_help("status", connector="discord")]
