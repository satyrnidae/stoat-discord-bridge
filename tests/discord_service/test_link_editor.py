"""`LinkEditorView` (services/discord_service/editor.py) - the discord.ui
in-line control panel on a `/link`/`/mirror` reply (issue #115). Exercises
the view's component wiring directly (`.callback(interaction)` /
`.on_submit(interaction)`, no real gateway) against a real `ChannelLinker`/
`RoleLinker` backed by the in-memory `fake_db`, so the assertions cover the
actual link/unlink state changes, not a hand-simulated double.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from stoat_discord_bridge.admin_commands import ChannelLinker, ConnectorInfo, LinkError, RoleLinker
from stoat_discord_bridge.services.discord_service.editor import _ENTER_MANUALLY, LinkEditorSpec, LinkEditorView
from stoat_discord_bridge.storage.channel_mappings import ChannelMappingRepository
from stoat_discord_bridge.storage.role_mappings import RoleMappingRepository


class _Response:
    def __init__(self) -> None:
        self.edits: list[tuple[str | None, object]] = []
        self.messages: list[tuple[str, bool]] = []
        self.modals: list[object] = []

    async def edit_message(self, *, content=None, view=None):
        self.edits.append((content, view))

    async def send_message(self, content, *, ephemeral=False):
        self.messages.append((content, ephemeral))

    async def send_modal(self, modal):
        self.modals.append(modal)


class _Interaction:
    def __init__(self, user_id: int = 1, manage_guild: bool = True) -> None:
        self.user = SimpleNamespace(id=user_id, guild_permissions=SimpleNamespace(manage_guild=manage_guild))
        self.response = _Response()


class _Message:
    def __init__(self) -> None:
        self.edit_calls: list[dict] = []

    async def edit(self, **kwargs):
        self.edit_calls.append(kwargs)


async def _list_stoat_channels():
    return [("s1", "general"), ("s2", "off-topic")]


def _channel_connectors():
    return {
        "discord": ConnectorInfo(id="discord", label="Discord"),
        "stoat": ConnectorInfo(id="stoat", label="Stoat", list_channels=_list_stoat_channels),
        "irc": ConnectorInfo(id="irc", label="IRC"),
    }


async def _linked_channel(fake_db, connectors):
    channel_mappings = ChannelMappingRepository(fake_db)
    linker = ChannelLinker(channel_mappings, connectors)
    await linker.link_channel(
        local_connector="discord", local_channel_id="d1", local_channel_name="general",
        source="stoat", source_id="s1", destination_id=None,
    )
    spec = LinkEditorSpec(
        kind="channel", linker=linker, local_connector="discord", local_id="d1",
        local_name="general", edited_connector="stoat",
    )
    return linker, channel_mappings, spec


# ---------------------------------------------------------------- create / seeding


async def test_create_seeds_connector_and_counterpart_selects(fake_db):
    connectors = _channel_connectors()
    _linker, _mappings, spec = await _linked_channel(fake_db, connectors)

    view = await LinkEditorView.create(spec, invoker_id=1, content="Linked.")

    assert view is not None
    connector_select, counterpart_select, unlink_button = view.children

    assert {o.value for o in connector_select.options} == {"stoat", "irc"}
    assert next(o for o in connector_select.options if o.value == "stoat").default is True
    assert next(o for o in connector_select.options if o.value == "irc").default is False

    assert {o.value for o in counterpart_select.options} == {"s1", "s2", _ENTER_MANUALLY}
    assert next(o for o in counterpart_select.options if o.value == "s1").default is True

    assert unlink_button.label == "Unlink"


async def test_create_returns_none_when_the_edge_does_not_exist(fake_db):
    connectors = _channel_connectors()
    channel_mappings = ChannelMappingRepository(fake_db)
    linker = ChannelLinker(channel_mappings, connectors)
    spec = LinkEditorSpec(
        kind="channel", linker=linker, local_connector="discord", local_id="d1",
        local_name="general", edited_connector="stoat",
    )

    view = await LinkEditorView.create(spec, invoker_id=1, content="Linked.")

    assert view is None


# ---------------------------------------------------------------- connector select (no commit)


async def test_connector_select_change_repopulates_counterpart_without_committing(fake_db):
    connectors = _channel_connectors()
    _linker, channel_mappings, spec = await _linked_channel(fake_db, connectors)
    view = await LinkEditorView.create(spec, invoker_id=1, content="Linked.")
    interaction = _Interaction()

    await view.change_pending_connector(interaction, "irc")

    assert view.edited_connector == "stoat"  # unchanged - nothing committed yet
    assert view.pending_connector == "irc"
    counterpart_select = view.children[1]
    # IRC wires no list_channels hook -> only the escape hatch is offered
    assert {o.value for o in counterpart_select.options} == {_ENTER_MANUALLY}
    assert view.content == "Linked."  # content untouched by a pending-only change
    assert len(interaction.response.edits) == 1

    group = await channel_mappings.get_bridge_group("stoat", "s1")
    assert group is not None  # still linked - nothing was committed


# ---------------------------------------------------------------- counterpart select (commits)


async def test_counterpart_select_apply_retargets_to_a_different_entity(fake_db):
    connectors = _channel_connectors()
    _linker, channel_mappings, spec = await _linked_channel(fake_db, connectors)
    view = await LinkEditorView.create(spec, invoker_id=1, content="Linked.")
    interaction = _Interaction()

    await view.apply_retarget(interaction, "s2")

    assert view.edited_connector == "stoat"
    group = await channel_mappings.get_bridge_group("discord", "d1")
    mapped = await channel_mappings.get_mapped_channels(group)
    assert {m.channel_id for m in mapped} == {"d1", "s2"}
    assert await channel_mappings.get_bridge_group("stoat", "s1") is None  # old edge dropped
    content, sent_view = interaction.response.edits[-1]
    assert "Linked" in content
    assert sent_view is view


async def test_retarget_to_a_different_connector_moves_the_whole_edge(fake_db):
    connectors = _channel_connectors()
    _linker, channel_mappings, spec = await _linked_channel(fake_db, connectors)
    view = await LinkEditorView.create(spec, invoker_id=1, content="Linked.")
    await view.change_pending_connector(_Interaction(), "irc")

    await view.apply_retarget(_Interaction(), "#lobby")

    assert view.edited_connector == "irc"
    group = await channel_mappings.get_bridge_group("discord", "d1")
    mapped = await channel_mappings.get_mapped_channels(group)
    assert {m.connector_id for m in mapped} == {"discord", "irc"}
    assert await channel_mappings.get_bridge_group("stoat", "s1") is None


async def test_counterpart_select_enter_manually_opens_a_modal_that_retargets(fake_db):
    connectors = _channel_connectors()
    _linker, channel_mappings, spec = await _linked_channel(fake_db, connectors)
    view = await LinkEditorView.create(spec, invoker_id=1, content="Linked.")
    counterpart_select = view.children[1]
    counterpart_select._values = [_ENTER_MANUALLY]
    open_modal_interaction = _Interaction()

    await counterpart_select.callback(open_modal_interaction)

    assert len(open_modal_interaction.response.modals) == 1
    modal = open_modal_interaction.response.modals[0]
    modal.value._value = "s2"
    submit_interaction = _Interaction()

    await modal.on_submit(submit_interaction)

    group = await channel_mappings.get_bridge_group("discord", "d1")
    mapped = await channel_mappings.get_mapped_channels(group)
    assert {m.channel_id for m in mapped} == {"d1", "s2"}


async def test_apply_retarget_conflict_drops_the_old_edge_and_reports_the_error(fake_db):
    """apply_retarget always drops the old edge before (re)linking the new
    one - the same "unlink, then link" two commands an operator would run by
    hand (issue #115's answer to its own open question #2) - so a conflict
    on the *new* side still leaves the old edge gone, not restored. Reached
    when the local anchor's group has a third member the pre-emptive unlink
    doesn't strand (so it isn't dissolved), and the new target already
    belongs to a different, unrelated group."""
    connectors = _channel_connectors()
    _linker, channel_mappings, spec = await _linked_channel(fake_db, connectors)
    # A third member keeps group A alive once the stoat edge is dropped.
    await _linker.link_channel(
        local_connector="irc", local_channel_id="#general", local_channel_name="#general",
        source="discord", source_id="d1", destination_id=None,
    )
    # An unrelated group B, whose stoat member ("s2") the retarget will collide with.
    await _linker.link_channel(
        local_connector="discord", local_channel_id="d2", local_channel_name="other",
        source="stoat", source_id="s2", destination_id=None,
    )
    view = await LinkEditorView.create(spec, invoker_id=1, content="Linked.")

    interaction = _Interaction()
    await view.apply_retarget(interaction, "s2")

    assert interaction.response.messages  # an ephemeral error, not an edit
    assert not interaction.response.edits
    assert "different bridge groups" in interaction.response.messages[0][0]
    # the old stoat edge is gone (the pre-emptive unlink isn't rolled back on failure) ...
    assert await channel_mappings.get_bridge_group("stoat", "s1") is None
    # ... but the rest of group A, and group B, are untouched
    group_a = await channel_mappings.get_bridge_group("discord", "d1")
    assert {m.channel_id for m in await channel_mappings.get_mapped_channels(group_a)} == {"d1", "#general"}
    group_b = await channel_mappings.get_bridge_group("discord", "d2")
    assert {m.channel_id for m in await channel_mappings.get_mapped_channels(group_b)} == {"d2", "s2"}


# ---------------------------------------------------------------- unlink button


async def test_unlink_button_reaches_a_terminal_state(fake_db):
    connectors = _channel_connectors()
    _linker, channel_mappings, spec = await _linked_channel(fake_db, connectors)
    view = await LinkEditorView.create(spec, invoker_id=1, content="Linked.")
    unlink_button = view.children[2]
    interaction = _Interaction()

    await unlink_button.callback(interaction)

    assert view.children == []
    assert view.is_finished()
    content, sent_view = interaction.response.edits[-1]
    assert "Unlinked" in content
    assert await channel_mappings.get_bridge_group("discord", "d1") is None


# ---------------------------------------------------------------- authorization


async def test_non_invoker_click_is_refused(fake_db):
    connectors = _channel_connectors()
    _linker, channel_mappings, spec = await _linked_channel(fake_db, connectors)
    view = await LinkEditorView.create(spec, invoker_id=1, content="Linked.")
    interaction = _Interaction(user_id=999)

    await view.apply_unlink(interaction)

    assert "Only the person who ran this command" in interaction.response.messages[0][0]
    assert not interaction.response.edits
    assert await channel_mappings.get_bridge_group("discord", "d1") is not None


async def test_click_without_manage_guild_is_refused(fake_db):
    connectors = _channel_connectors()
    _linker, channel_mappings, spec = await _linked_channel(fake_db, connectors)
    view = await LinkEditorView.create(spec, invoker_id=1, content="Linked.")
    interaction = _Interaction(manage_guild=False)

    await view.apply_unlink(interaction)

    assert "Manage Server" in interaction.response.messages[0][0]
    assert await channel_mappings.get_bridge_group("discord", "d1") is not None


# ---------------------------------------------------------------- timeout


async def test_on_timeout_disables_every_item_and_edits_the_message(fake_db):
    connectors = _channel_connectors()
    _linker, _mappings, spec = await _linked_channel(fake_db, connectors)
    view = await LinkEditorView.create(spec, invoker_id=1, content="Linked.")
    message = _Message()
    view.message = message

    await view.on_timeout()

    assert all(item.disabled for item in view.children)
    assert message.edit_calls == [{"view": view}]


# ---------------------------------------------------------------- role kind (a second entity shape)


async def test_role_kind_retargets_via_the_same_view(fake_db):
    connectors = {
        "discord": ConnectorInfo(id="discord", label="Discord"),
        "stoat": ConnectorInfo(id="stoat", label="Stoat"),
    }
    role_mappings = RoleMappingRepository(fake_db)
    linker = RoleLinker(role_mappings, connectors)
    await linker.link_role(local_connector="discord", local_role="d-role", source="stoat", source_role="s-role")
    spec = LinkEditorSpec(
        kind="role", linker=linker, local_connector="discord", local_id="d-role",
        local_name="Moderators", edited_connector="stoat",
    )

    view = await LinkEditorView.create(spec, invoker_id=1, content="Linked.")
    assert view is not None

    await view.apply_retarget(_Interaction(), "s-role-2")

    group = await role_mappings.get_bridge_group("discord", "d-role")
    mapped = await role_mappings.get_mapped_roles(group)
    assert {m.role_id for m in mapped} == {"d-role", "s-role-2"}
