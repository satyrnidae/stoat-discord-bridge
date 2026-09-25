"""`/attachments prefer|unprefer|preferences` handlers on Stoat (issue #164)."""

from __future__ import annotations

from tests.stoat_service.conftest import _make_ctx, _make_sender


class _FakeManager:
    def __init__(self):
        self.prefer_calls: list[dict] = []
        self.unprefer_calls: list[dict] = []

    async def prefer(self, **kwargs):
        self.prefer_calls.append(kwargs)
        return "Links containing 'instagram.com' will now use stoat's own preview."

    async def unprefer(self, **kwargs):
        self.unprefer_calls.append(kwargs)
        return "Removed the preference for 'instagram.com'."

    async def list_preferences(self):
        return "Attachment preferences:\ninstagram.com -> stoat"


def _sender(manager):
    sender = _make_sender()
    sender._attachment_preferences = manager
    return sender


async def test_prefer_forwards_kind_and_substring():
    manager = _FakeManager()
    ctx = _make_ctx()

    await _sender(manager)._attachments_prefer(ctx, "stoat", "instagram.com")

    assert manager.prefer_calls == [{"kind": "stoat", "url_substring": "instagram.com"}]
    assert ctx.channel.sent[0]["content"] == "Links containing 'instagram.com' will now use stoat's own preview."


async def test_prefer_needs_admin_permission():
    manager = _FakeManager()
    ctx = _make_ctx(manage_server=False)

    await _sender(manager)._attachments_prefer(ctx, "stoat", "instagram.com")

    assert ctx.channel.sent[0]["content"] == "You need the Manage Server permission to do that."
    assert manager.prefer_calls == []


async def test_unprefer_forwards_the_substring():
    manager = _FakeManager()
    ctx = _make_ctx()

    await _sender(manager)._attachments_unprefer(ctx, "instagram.com")

    assert manager.unprefer_calls == [{"url_substring": "instagram.com"}]
    assert ctx.channel.sent[0]["content"] == "Removed the preference for 'instagram.com'."


async def test_unprefer_needs_admin_permission():
    manager = _FakeManager()
    ctx = _make_ctx(manage_server=False)

    await _sender(manager)._attachments_unprefer(ctx, "instagram.com")

    assert manager.unprefer_calls == []


async def test_preferences_needs_no_admin_permission():
    ctx = _make_ctx(manage_server=False)

    await _sender(_FakeManager())._attachments_preferences(ctx)

    assert ctx.channel.sent[0]["content"] == "Attachment preferences:\ninstagram.com -> stoat"


async def test_every_subcommand_reports_when_unconfigured():
    ctx = _make_ctx()
    sender = _sender(None)

    await sender._attachments_prefer(ctx, "stoat", "instagram.com")
    await sender._attachments_unprefer(ctx, "instagram.com")
    await sender._attachments_preferences(ctx)

    assert [m["content"] for m in ctx.channel.sent] == ["Attachment preferences aren't configured."] * 3
