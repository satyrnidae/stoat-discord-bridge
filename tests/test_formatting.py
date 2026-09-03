from datetime import datetime, timezone

import pytest

from stoat_discord_bridge.models import Attachment, StandardMessage
from stoat_discord_bridge.services.formatting import (
    chunk_content,
    content_with_attachments,
    render_discord_timestamps,
    strip_markdown,
)

# 2026-08-29 22:00:00 UTC
_EPOCH = 1788040800
_NOW = datetime(2026, 8, 29, 22, 0, 0, tzinfo=timezone.utc)


def _message(content="hello", attachments=None):
    return StandardMessage(
        origin_connector_id="discord",
        origin_channel_id="c1",
        channel_name="general",
        sender_name="Alice",
        sender_avatar_url=None,
        sender_user_id="alice-id",
        content_markdown=content,
        message_id="m1",
        attachments=attachments or [],
    )


def test_content_with_attachments_appends_urls():
    message = _message("hello", [Attachment(url="https://example.com/a.png")])
    assert content_with_attachments(message) == "hello\nhttps://example.com/a.png"


def test_content_with_attachments_no_attachments():
    assert content_with_attachments(_message("hello", [])) == "hello"


def test_content_with_attachments_empty_message_uses_zero_width_space():
    assert content_with_attachments(_message("", [])) == "​"


def test_chunk_content_under_limit_is_one_chunk():
    assert chunk_content("short", 100) == ["short"]


def test_chunk_content_splits_on_line_boundaries_when_possible():
    content = "a" * 10 + "\n" + "b" * 10
    chunks = chunk_content(content, 15)
    assert "".join(chunks) == content
    assert all(len(c) <= 15 for c in chunks)


def test_chunk_content_hard_splits_a_line_longer_than_the_limit():
    content = "x" * 50
    chunks = chunk_content(content, 20)
    assert "".join(chunks) == content
    assert all(len(c) <= 20 for c in chunks)


def test_chunk_content_empty_string_returns_one_chunk():
    assert chunk_content("", 100) == [""]


@pytest.mark.parametrize(
    ("style", "expected"),
    [
        ("t", "10:00 PM UTC"),
        ("T", "10:00:00 PM UTC"),
        ("d", "8/29/2026"),
        ("D", "August 29, 2026"),
        ("f", "August 29, 2026 10:00 PM UTC"),
        ("F", "Saturday, August 29, 2026 10:00:00 PM UTC"),
    ],
)
def test_render_discord_timestamps_absolute_styles(style, expected):
    assert render_discord_timestamps(f"<t:{_EPOCH}:{style}>", now=_NOW) == expected


def test_render_discord_timestamps_default_style_is_short_datetime():
    assert render_discord_timestamps(f"<t:{_EPOCH}>", now=_NOW) == "August 29, 2026 10:00 PM UTC"


def test_render_discord_timestamps_relative_future():
    assert render_discord_timestamps(f"<t:{_EPOCH + 120}:R>", now=_NOW) == "in 2 minutes"


def test_render_discord_timestamps_relative_past():
    assert render_discord_timestamps(f"<t:{_EPOCH - 300000}:R>", now=_NOW) == "3 days ago"


def test_render_discord_timestamps_relative_singular_and_now():
    assert render_discord_timestamps(f"<t:{_EPOCH + 3600}:R>", now=_NOW) == "in 1 hour"
    assert render_discord_timestamps(f"<t:{_EPOCH}:R>", now=_NOW) == "now"


def test_render_discord_timestamps_leaves_out_of_range_token_untouched():
    token = "<t:99999999999999999999:F>"
    assert render_discord_timestamps(token, now=_NOW) == token


def test_render_discord_timestamps_ignores_plain_text_and_handles_multiple_tokens():
    assert render_discord_timestamps("no tokens here", now=_NOW) == "no tokens here"
    out = render_discord_timestamps(f"a <t:{_EPOCH}:d> b <t:{_EPOCH}:t> c", now=_NOW)
    assert out == "a 8/29/2026 b 10:00 PM UTC c"


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("**bold**", "bold"),
        ("*italic*", "italic"),
        ("__underline__", "underline"),
        ("~~strike~~", "strike"),
        ("||spoiler||", "spoiler"),
        ("***bold italic***", "bold italic"),
        ("a **b** and _c_ and `d`", "a b and c and d"),
        ("# Heading", "Heading"),
        ("## Heading 2", "Heading 2"),
        ("> quoted line", "quoted line"),
        ("plain text, nothing to do", "plain text, nothing to do"),
        ("keeps snake_case_names intact", "keeps snake_case_names intact"),
        (r"escaped \*not italic\*", "escaped *not italic*"),
    ],
)
def test_strip_markdown_basic(source, expected):
    assert strip_markdown(source) == expected


def test_strip_markdown_masked_link_appends_url():
    assert strip_markdown("see [the docs](https://example.com/x)") == "see the docs (https://example.com/x)"


def test_strip_markdown_masked_link_with_url_as_label_is_left_bare():
    assert (
        strip_markdown("[https://example.com/x](https://example.com/x)") == "https://example.com/x"
    )


def test_strip_markdown_inline_code_is_left_literal():
    assert strip_markdown("run `rm -rf *` now") == "run rm -rf * now"


def test_strip_markdown_fenced_code_block_keeps_contents():
    assert strip_markdown("```python\nx = **1**\n```") == "x = **1**"


def test_strip_markdown_does_not_mangle_underscores_in_a_bare_url():
    url = "https://example.com/foo_bar_baz.png"
    assert strip_markdown(url) == url
