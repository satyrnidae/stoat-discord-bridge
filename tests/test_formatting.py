from datetime import datetime, timezone

import aiohttp
import pytest

from stoat_discord_bridge.models import Attachment
from stoat_discord_bridge.services.formatting import (
    chunk_content,
    decorate_sender_name,
    download_attachments,
    inline_attachment_urls,
    render_discord_timestamps,
    strip_markdown,
)

# 2026-08-29 22:00:00 UTC
_EPOCH = 1788040800
_NOW = datetime(2026, 8, 29, 22, 0, 0, tzinfo=timezone.utc)


def test_inline_attachment_urls_appends_urls():
    assert (
        inline_attachment_urls("hello", [Attachment(url="https://example.com/a.png")])
        == "hello\nhttps://example.com/a.png"
    )


def test_inline_attachment_urls_no_attachments():
    assert inline_attachment_urls("hello", []) == "hello"


def test_inline_attachment_urls_empty_uses_zero_width_space():
    assert inline_attachment_urls("", []) == "​"


def test_inline_attachment_urls_skips_urlless_attachments():
    assert inline_attachment_urls("hi", [Attachment(url="")]) == "hi"


class _FakeAiohttpResponse:
    def __init__(self, body: bytes, *, status: int = 200) -> None:
        self._body = body
        self.status = status

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    def raise_for_status(self) -> None:
        if self.status >= 400:
            raise aiohttp.ClientResponseError(None, (), status=self.status)

    async def read(self) -> bytes:
        return self._body


async def test_download_attachments_fetches_bytes_and_names_them(monkeypatch):
    monkeypatch.setattr(aiohttp.ClientSession, "get", lambda self, url: _FakeAiohttpResponse(b"png-bytes"))

    downloaded, undownloadable = await download_attachments(
        [Attachment(url="https://cdn.example/x/photo.png", filename="photo.png")]
    )

    assert downloaded == [("photo.png", b"png-bytes")]
    assert undownloadable == []


async def test_download_attachments_derives_filename_from_url_when_missing(monkeypatch):
    monkeypatch.setattr(aiohttp.ClientSession, "get", lambda self, url: _FakeAiohttpResponse(b"x"))

    downloaded, _ = await download_attachments([Attachment(url="https://cdn.example/a/b/pic.jpg?ex=deadbeef")])

    assert downloaded == [("pic.jpg", b"x")]


async def test_download_attachments_falls_back_on_fetch_failure(monkeypatch):
    monkeypatch.setattr(
        aiohttp.ClientSession, "get", lambda self, url: _FakeAiohttpResponse(b"", status=404)
    )
    att = Attachment(url="https://cdn.example/gone.png")

    downloaded, undownloadable = await download_attachments([att])

    assert downloaded == []
    assert undownloadable == [att]


async def test_download_attachments_falls_back_when_too_large(monkeypatch):
    monkeypatch.setattr(aiohttp.ClientSession, "get", lambda self, url: _FakeAiohttpResponse(b"x" * 100))
    att = Attachment(url="https://cdn.example/big.zip", size_bytes=999_999_999)

    downloaded, undownloadable = await download_attachments([att], max_bytes=50)

    assert downloaded == []
    assert undownloadable == [att]


async def test_download_attachments_falls_back_when_downloaded_body_exceeds_limit(monkeypatch):
    monkeypatch.setattr(aiohttp.ClientSession, "get", lambda self, url: _FakeAiohttpResponse(b"x" * 100))
    att = Attachment(url="https://cdn.example/big.png")  # size unknown up front

    downloaded, undownloadable = await download_attachments([att], max_bytes=50)

    assert downloaded == []
    assert undownloadable == [att]


async def test_download_attachments_empty_list_is_a_noop():
    assert await download_attachments([]) == ([], [])


def test_decorate_sender_name_no_extras_is_unchanged():
    assert decorate_sender_name("saturniidae") == "saturniidae"


def test_decorate_sender_name_source_only():
    assert decorate_sender_name("saturniidae", source="Discord") == "saturniidae [Discord]"


def test_decorate_sender_name_pronouns_only():
    assert decorate_sender_name("saturniidae", pronouns="she/her") == "saturniidae [she/her]"


def test_decorate_sender_name_source_and_pronouns():
    assert (
        decorate_sender_name("saturniidae", source="Stoat (public)", pronouns="she/her")
        == "saturniidae [Stoat (public), she/her]"
    )


def test_decorate_sender_name_max_len_keeps_the_suffix_when_it_fits():
    assert (
        decorate_sender_name("sat", source="IRC", pronouns="she/her", max_len=32)
        == "sat [IRC, she/her]"
    )


def test_decorate_sender_name_max_len_drops_the_suffix_whole_when_it_overflows():
    out = decorate_sender_name("a-long-enough-display-name", source="Stoat (public)", max_len=32)
    assert out == "a-long-enough-display-name"
    assert "[" not in out


def test_decorate_sender_name_max_len_clips_a_bare_name_with_no_suffix():
    assert decorate_sender_name("x" * 40, max_len=32) == "x" * 32


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
