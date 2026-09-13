"""Discord's `/help [topic]` command (issue #117) - registration, the no-arg
index, and topic drill-down. Renders via the shared `admin_commands.help`
table every connector's help command draws from.
"""

from __future__ import annotations

from discord import app_commands

from stoat_discord_bridge.admin_commands import HELP_TOPICS, render_help
from tests.discord_service.conftest import FakeInteraction, FakeLinker, _make_sender


def _help_command(sender):
    return sender.tree.get_command("help", guild=sender._guild)


async def test_help_command_is_registered_with_every_topic_as_a_choice():
    sender = _make_sender(FakeLinker())
    command = _help_command(sender)

    assert command is not None
    [topic_param] = [p for p in command.parameters if p.name == "topic"]
    assert {choice.value for choice in topic_param.choices} == set(HELP_TOPICS)


async def test_help_command_with_no_topic_sends_the_index():
    sender = _make_sender(FakeLinker())
    interaction = FakeInteraction()

    await _help_command(sender).callback(interaction, topic=None)

    assert interaction.sent == [render_help(None, connector="discord")]


async def test_help_command_with_a_topic_drills_down():
    sender = _make_sender(FakeLinker())
    interaction = FakeInteraction()

    await _help_command(sender).callback(
        interaction, topic=app_commands.Choice(name="x", value="link channel")
    )

    assert interaction.sent == [render_help("link channel", connector="discord")]
