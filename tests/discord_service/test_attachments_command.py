"""`/attachments prefer|unprefer|preferences` on Discord (issue #164)."""

from __future__ import annotations

from stoat_discord_bridge.admin_commands import LinkError
from tests.discord_service.conftest import FakeAttachmentPreferenceManager, FakeInteraction, FakeLinker, _make_sender


def test_the_attachments_group_is_registered():
    sender = _make_sender(FakeLinker())
    group = sender.tree.get_command("attachments", guild=sender._guild)
    assert {c.name for c in group.commands} == {"prefer", "unprefer", "preferences"}
    assert group.default_permissions.manage_guild


async def test_prefer_forwards_kind_and_substring():
    manager = FakeAttachmentPreferenceManager()
    sender = _make_sender(FakeLinker(), attachment_preferences=manager)
    interaction = FakeInteraction()

    await sender._handle_attachments_prefer(interaction, "stoat", "instagram.com")

    assert manager.prefer_calls == [{"kind": "stoat", "url_substring": "instagram.com"}]
    assert interaction.sent == ["Links containing 'instagram.com' will now use stoat's own preview."]


async def test_prefer_reports_a_link_error():
    manager = FakeAttachmentPreferenceManager()

    async def reject(**kwargs):
        raise LinkError("the URL substring can't be empty.")

    manager.prefer = reject
    sender = _make_sender(FakeLinker(), attachment_preferences=manager)
    interaction = FakeInteraction()

    await sender._handle_attachments_prefer(interaction, "stoat", " ")

    assert interaction.sent == ["the URL substring can't be empty."]


async def test_unprefer_forwards_the_substring():
    manager = FakeAttachmentPreferenceManager()
    sender = _make_sender(FakeLinker(), attachment_preferences=manager)
    interaction = FakeInteraction()

    await sender._handle_attachments_unprefer(interaction, "instagram.com")

    assert manager.unprefer_calls == [{"url_substring": "instagram.com"}]
    assert interaction.sent == ["Removed the preference for 'instagram.com'."]


async def test_preferences_lists_the_rules():
    manager = FakeAttachmentPreferenceManager()
    sender = _make_sender(FakeLinker(), attachment_preferences=manager)
    interaction = FakeInteraction()

    await sender._handle_attachments_preferences(interaction)

    assert interaction.sent == ["Attachment preferences:\ninstagram.com -> stoat"]


async def test_every_subcommand_reports_when_unconfigured():
    sender = _make_sender(FakeLinker(), attachment_preferences=None)
    interaction = FakeInteraction()

    await sender._handle_attachments_prefer(interaction, "stoat", "instagram.com")
    await sender._handle_attachments_unprefer(interaction, "instagram.com")
    await sender._handle_attachments_preferences(interaction)

    assert interaction.sent == ["Attachment preferences aren't configured."] * 3
