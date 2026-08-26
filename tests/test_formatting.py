from stoat_discord_bridge.models import Attachment, StandardMessage
from stoat_discord_bridge.services.formatting import chunk_content, content_with_attachments


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
