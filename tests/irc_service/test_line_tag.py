"""`parse_relayed_line` - reading the IRC receiver's `<nick[, Source][,
pronouns]> text` line tag back into a sender and content (issue #161)."""

import pytest

from stoat_discord_bridge.services.irc_service.formatting import parse_relayed_line


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("<alice> hi", ("alice", "hi")),
        ("<alice, Discord> hi", ("alice [Discord]", "hi")),
        ("<alice, Discord, she/her> hi there", ("alice [Discord, she/her]", "hi there")),
        ("<alice, Stoat (public)> <b>not a tag</b>", ("alice [Stoat (public)]", "<b>not a tag</b>")),
        ("<alice> ", ("alice", "")),
    ],
)
def test_parses_a_tagged_line(line, expected):
    assert parse_relayed_line(line) == expected


@pytest.mark.parametrize("line", ["plain text", "<> empty tag", "<alice>no space", ""])
def test_returns_none_for_an_untagged_line(line):
    assert parse_relayed_line(line) is None
