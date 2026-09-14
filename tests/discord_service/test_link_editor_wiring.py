"""Wiring-level tests for the in-line link editor (issue #115): that the
`_handle_link_*` / `_handle_mirror_*` / `_handle_linked_*` handlers in
`services/discord_service/linking.py` actually attach a `LinkEditorView` to
a successful reply, gate the `/linked <noun>` "Edit" view on Manage Server,
and never attach one to an error or empty-mirror reply. The view's own
component behavior (retarget/unlink) is covered by `test_link_editor.py`
against real linkers - this file only exercises the *wiring*, so it sticks
to the `channel` kind as representative risk coverage (the mechanism is
kind-agnostic, per LinkEditorSpec/`_ADAPTERS`).
"""

from __future__ import annotations

from types import SimpleNamespace

from stoat_discord_bridge.admin_commands import LinkedMember
from stoat_discord_bridge.services.discord_service.editor import LinkEditorView
from tests.discord_service.conftest import FakeInteraction, FakeLinker, _make_sender


def _stoat_group() -> tuple[str, list[LinkedMember]]:
    # A `describe_group` result carrying a "stoat" member - all
    # LinkEditorView.create needs to seed itself (FakeLinker.describe_group
    # ignores its arguments and always returns this, so the exact ids don't
    # have to match the handler's own resolved anchor).
    return (
        "group1",
        [
            LinkedMember(connector_id="discord", label="Discord", entity_id="999", name="current-channel"),
            LinkedMember(connector_id="stoat", label="Stoat", entity_id="s1", name="general"),
        ],
    )


async def test_link_channel_attaches_an_editor_view_on_success():
    linker = FakeLinker()
    linker.describe_group_result = _stoat_group()
    sender = _make_sender(linker)
    interaction = FakeInteraction()

    await sender._handle_link_channel(interaction, "stoat", "s1", None)

    assert interaction.sent == ["ok"]
    [view] = interaction.sent_views
    assert isinstance(view, LinkEditorView)
    assert view.spec.kind == "channel"
    assert view.spec.local_connector == "discord"
    assert view.spec.edited_connector == "stoat"


async def test_mirror_channel_all_attaches_no_editor_view():
    # The "all" fan-out has no single counterpart to re-target/unlink, so
    # it's deliberately left without an editor (see linking.py's
    # _handle_mirror_channel).
    linker = FakeLinker()
    sender = _make_sender(linker)
    interaction = FakeInteraction()

    await sender._handle_mirror_channel(interaction, "all", None)

    assert interaction.sent == ["ok"]
    assert interaction.sent_views == [None]


async def test_mirror_channel_to_one_destination_attaches_an_editor_view():
    linker = FakeLinker()
    linker.describe_group_result = _stoat_group()
    sender = _make_sender(linker)
    interaction = FakeInteraction()

    await sender._handle_mirror_channel(interaction, "stoat", None)

    assert interaction.sent == ["ok"]
    [view] = interaction.sent_views
    assert isinstance(view, LinkEditorView)
    assert view.spec.edited_connector == "stoat"


async def test_mirror_channel_attaches_no_editor_view_on_empty_result():
    class _EmptyLinker(FakeLinker):
        async def mirror_channel(self, **kwargs):
            await super().mirror_channel(**kwargs)
            return ""

    linker = _EmptyLinker()
    sender = _make_sender(linker)
    interaction = FakeInteraction()

    await sender._handle_mirror_channel(interaction, "stoat", None)

    assert interaction.sent == ["Nothing to mirror."]
    assert interaction.sent_views == [None]


async def test_linked_channels_attaches_no_edit_view_when_group_unlinked():
    linker = FakeLinker()  # describe_group_result stays None: unlinked
    sender = _make_sender(linker)
    interaction = FakeInteraction()

    await sender._handle_linked_channels(interaction, None)

    assert interaction.sent_views == [None]


async def test_linked_channels_attaches_an_edit_view_when_group_exists_and_invoker_can_manage_guild():
    linker = FakeLinker()
    linker.describe_group_result = (
        "group1",
        [
            LinkedMember(connector_id="discord", label="Discord", entity_id="999", name="general"),
            LinkedMember(connector_id="stoat", label="Stoat", entity_id="s1", name="general-stoat"),
        ],
    )
    sender = _make_sender(linker)
    interaction = FakeInteraction()
    interaction.user.guild_permissions = SimpleNamespace(manage_guild=True)

    await sender._handle_linked_channels(interaction, None)

    [view] = interaction.sent_views
    assert isinstance(view, LinkEditorView)
    assert view.spec.local_id == "999"
    assert view.spec.edited_connector == "stoat"


async def test_linked_channels_attaches_no_edit_view_when_invoker_cannot_manage_guild():
    linker = FakeLinker()
    linker.describe_group_result = (
        "group1",
        [
            LinkedMember(connector_id="discord", label="Discord", entity_id="999", name="general"),
            LinkedMember(connector_id="stoat", label="Stoat", entity_id="s1", name="general-stoat"),
        ],
    )
    sender = _make_sender(linker)
    interaction = FakeInteraction()
    interaction.user.guild_permissions = SimpleNamespace(manage_guild=False)

    await sender._handle_linked_channels(interaction, None)

    assert interaction.sent_views == [None]


async def test_link_channel_reply_has_no_editor_view_on_a_linkerror():
    class _RejectingLinker(FakeLinker):
        async def link_channel(self, **kwargs):
            from stoat_discord_bridge.admin_commands import LinkError

            raise LinkError("nope")

    linker = _RejectingLinker()
    sender = _make_sender(linker)
    interaction = FakeInteraction()

    await sender._handle_link_channel(interaction, "stoat", "s1", None)

    assert interaction.sent == ["nope"]
    assert interaction.sent_views == [None]
