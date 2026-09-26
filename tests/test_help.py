"""Tests for the shared help content behind Discord's `/help`, Stoat's
`/bridge-help`, and IRC's `HELP` (issue #117) - `admin_commands/help.py`.
"""

from __future__ import annotations

import pytest

from stoat_discord_bridge.admin_commands import HELP_TOPICS, render_help, resolve_help_key

_CONNECTORS = ("discord", "stoat", "irc")


# ---------------------------------------------------------------- resolve_help_key


@pytest.mark.parametrize(
    "topic, noun, expected",
    [
        (None, None, ""),
        ("status", None, "status"),
        ("link", "channel", "link channel"),
        (" Link ", " Channel ", "link channel"),
        ("MIRROR", "ROLE", "mirror role"),
        ("", "", ""),
    ],
)
def test_resolve_help_key(topic, noun, expected):
    assert resolve_help_key(topic, noun) == expected


# ---------------------------------------------------------------- render_help: index


def test_index_when_topic_is_none():
    text = render_help(None, connector="discord")
    assert text.startswith("Bridge commands (see COMMANDS.md for full detail):")
    assert "/link channel" in text
    assert "/help [topic] [noun] - this message" in text


def test_index_when_topic_is_unrecognized():
    assert render_help("not-a-real-topic", connector="discord") == render_help(None, connector="discord")


@pytest.mark.parametrize("connector", _CONNECTORS)
def test_index_lists_every_topic_this_connector_offers(connector):
    text = render_help(None, connector=connector)
    for key, entry in HELP_TOPICS.items():
        if entry.syntax.get(connector) is None:
            continue
        assert entry.summary in text, f"{key} missing from the {connector} index"


def test_irc_index_omits_role_category_emote_topics():
    text = render_help(None, connector="irc")
    for key in ("link role", "link category", "link emote", "mirror role", "mirror category", "mirror emote"):
        assert HELP_TOPICS[key].summary not in text


def test_stoat_index_uses_the_given_prefix():
    text = render_help(None, connector="stoat", prefix="!")
    assert "!status" in text
    assert "!bridge-help" in text
    assert "/status" not in text


def test_index_ends_with_this_connectors_own_help_command():
    assert render_help(None, connector="discord").rstrip().endswith("/help [topic] [noun] - this message")
    assert render_help(None, connector="stoat").rstrip().endswith("/bridge-help [topic] [noun] - this message")
    assert render_help(None, connector="irc").rstrip().endswith("HELP [topic] [noun] - this message")


# ---------------------------------------------------------------- render_help: topic detail


def test_topic_detail_includes_syntax_body_and_permission():
    text = render_help("link channel", connector="discord")
    entry = HELP_TOPICS["link channel"]
    assert text.startswith(entry.syntax["discord"])
    assert entry.body in text
    assert f"Permission: {entry.permission}" in text


def test_topic_detail_falls_back_to_index_when_unsupported_on_this_connector():
    # "link role" has no IRC syntax - IRC has no role concept.
    assert render_help("link role", connector="irc") == render_help(None, connector="irc")


@pytest.mark.parametrize("key", list(HELP_TOPICS))
def test_every_topic_key_renders_on_discord(key):
    # Every topic is offered on Discord (the connector this issue adds a
    # help command to) - none should silently fall back to the index there.
    text = render_help(key, connector="discord")
    assert text != render_help(None, connector="discord")


def test_status_topic_is_a_single_token_key_with_no_noun():
    text = render_help(resolve_help_key("status"), connector="irc")
    assert text.startswith("STATUS")
    assert "Permission: read-only" in text


def test_mirror_channel_topic_key_combines_verb_and_noun():
    text = render_help(resolve_help_key("mirror", "channel"), connector="stoat", prefix="/")
    assert text.startswith("/mirror channel to")


def test_stoat_topic_detail_uses_the_given_prefix():
    text = render_help("link channel", connector="stoat", prefix="!")
    assert text.startswith("!link channel")


@pytest.mark.parametrize(
    "connector, expected",
    [
        ("discord", "/import <service> <external_channel> [local_channel] [history_limit]"),
        ("stoat", "!import <service> <external_channel|name> [local_channel|name] [limit:<n|all>]"),
        ("irc", "IMPORT <service> <external_channel> <local_channel> [-l|--limit <n|all>]"),
    ],
)
def test_import_topic_on_every_connector(connector, expected):
    # issue #161 - IRC's HELP IMPORT is a single-token key, like STATUS.
    text = render_help(resolve_help_key("import"), connector=connector, prefix="!")
    assert text.startswith(expected)


@pytest.mark.parametrize("connector", _CONNECTORS)
def test_export_topic_on_every_connector(connector):
    text = render_help("export", connector=connector)
    assert text.lower().lstrip("/").startswith("export <service> <external_channel")
    assert "Permission:" in text


def test_topic_verbs_fit_discords_25_choice_cap():
    # Discord's /help offers one static choice per verb, with the noun
    # autocompleted (issue #172) - so only the verb count is capped.
    assert len({key.split(" ", 1)[0] for key in HELP_TOPICS}) <= 25


@pytest.mark.parametrize(
    "connector, expected",
    [
        ("discord", "/unlink all <service|all>"),
        ("stoat", "!unlink all <service|all>"),
        ("irc", "UNLINK ALL <service|all>"),
    ],
)
def test_unlink_all_topic_on_every_connector(connector, expected):
    text = render_help(resolve_help_key("unlink", "all"), connector=connector, prefix="!")
    assert text.startswith(expected)


@pytest.mark.parametrize(
    "noun, expected",
    [
        ("user", "/unlink user [service|all] [local_id|all]"),
        ("role", "/unlink role <local_id|all> [service|all]"),
        ("category", "/unlink category [local_id|all] [service|all]"),
        ("emote", "/unlink emote <local_id|all> [service|all]"),
    ],
)
def test_unlink_noun_syntax_offers_all_as_local_id(noun, expected):
    assert render_help(f"unlink {noun}", connector="discord").startswith(expected)


@pytest.mark.parametrize("connector", ["discord", "stoat"])
def test_attachments_topic_on_discord_and_stoat(connector):
    text = render_help("attachments", connector=connector)
    assert text.lstrip("/").startswith("attachments prefer")
    assert "unprefer" in text and "preferences" in text


def test_attachments_topic_is_not_offered_on_irc():
    assert "attachments" not in render_help(None, connector="irc")
