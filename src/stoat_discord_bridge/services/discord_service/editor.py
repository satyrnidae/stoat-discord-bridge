"""`LinkEditorView` - the in-line `discord.ui` control panel attached to a
successful `/link` / `/mirror` reply (and, via `/linked <noun>`'s Edit
button, to an existing group), so an operator can re-target the link that
was just created without retyping the command (issue #115).

First cut, per the issue's own open question #3: a **connector** select (move
this one edge to a different connector) and a **counterpart** select (change
which entity on that connector it's linked to) - both backed entirely by
existing linker methods (`link_<kind>` / `unlink_<kind>`) - plus an
**Unlink** button. Rename and Category-move each need a new linker method
and are deliberately deferred to a follow-up rather than bundled in here.

This is the first `discord.ui` use in the project - there's no existing
pattern to extend. The panel edits exactly one edge of a bridge/link/mapping
group: the (local anchor) <-> (`edited_connector`) pair the launching
`/link`/`/mirror` command targeted, carried on `LinkEditorSpec`. A group with
more than two members is untouched beyond that one edge - this panel isn't a
full group editor.
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import discord

from stoat_discord_bridge.admin_commands import LinkError

if TYPE_CHECKING:
    from stoat_discord_bridge.admin_commands.common import ConnectorInfo

logger = logging.getLogger(__name__)

# discord.ui.Select allows at most 25 options; one slot is reserved for the
# "Enter an id/name..." escape hatch (issue #115's answer to its own open
# question #5 - no pagination in this first cut), so at most 24 real entities
# are offered before falling back to free text for the rest.
_MAX_SELECT_OPTIONS = 25
_ENTER_MANUALLY = "__link_editor_enter_manually__"

# How long the panel stays live before disabling itself - comfortably inside
# a Discord ephemeral message's lifetime, but short enough that a stale panel
# (the operator moved on) doesn't linger as a standing write surface.
_TIMEOUT_SECONDS = 300.0


@dataclass(frozen=True)
class LinkEditorSpec:
    """What `LinkEditorView` needs to edit one edge of a link: which linker
    drives it, the *local* anchor (immutable - the entity the launching
    `/link`/`/mirror` command was run against) and which connector that
    command just linked it to. Built by each `_handle_link_*`/`_handle_mirror_*`
    handler from the same arguments it used for the call itself."""

    kind: str  # "channel" | "category" | "role" | "user" | "emote"
    linker: Any
    local_connector: str
    local_id: str
    local_name: str
    edited_connector: str


@dataclass(frozen=True)
class _KindAdapter:
    key: str
    noun: str
    list_hook_name: str
    connector_supported: Callable[["ConnectorInfo"], bool]
    link: Callable[..., Awaitable[str]]
    unlink: Callable[..., Awaitable[str]]


async def _link_channel(linker: Any, *, local_connector: str, local_id: str, local_name: str, source: str, source_id: str) -> str:
    return await linker.link_channel(
        local_connector=local_connector, local_channel_id=local_id, local_channel_name=local_name,
        source=source, source_id=source_id, destination_id=None,
    )


async def _unlink_channel(linker: Any, *, local_connector: str, local_id: str, destination: str) -> str:
    return await linker.unlink_channel(local_connector=local_connector, local_channel_id=local_id, destination=destination)


async def _link_category(linker: Any, *, local_connector: str, local_id: str, local_name: str, source: str, source_id: str) -> str:
    return await linker.link_category(
        local_connector=local_connector, local_category_id=local_id, local_category_name=local_name,
        source=source, source_id=source_id, destination_id=None,
    )


async def _unlink_category(linker: Any, *, local_connector: str, local_id: str, destination: str) -> str:
    return await linker.unlink_category(local_connector=local_connector, local_category_id=local_id, destination=destination)


async def _link_role(linker: Any, *, local_connector: str, local_id: str, local_name: str, source: str, source_id: str) -> str:
    return await linker.link_role(local_connector=local_connector, local_role=local_id, source=source, source_role=source_id)


async def _unlink_role(linker: Any, *, local_connector: str, local_id: str, destination: str) -> str:
    return await linker.unlink_role(local_connector=local_connector, local_role=local_id, destination=destination)


async def _link_emote(linker: Any, *, local_connector: str, local_id: str, local_name: str, source: str, source_id: str) -> str:
    return await linker.link_emote(local_connector=local_connector, local_id=local_id, source=source, source_id=source_id)


async def _unlink_emote(linker: Any, *, local_connector: str, local_id: str, destination: str) -> str:
    return await linker.unlink_emote(local_connector=local_connector, local_emote=local_id, destination=destination)


async def _link_user(linker: Any, *, local_connector: str, local_id: str, local_name: str, source: str, source_id: str) -> str:
    return await linker.link_user(
        local_connector=local_connector, local_user_id=local_id, source=source, source_user_id=source_id
    )


async def _unlink_user(linker: Any, *, local_connector: str, local_id: str, destination: str) -> str:
    return await linker.unlink_user(local_connector=local_connector, local_user_id=local_id, destination=destination)


_ADAPTERS: dict[str, _KindAdapter] = {
    "channel": _KindAdapter("channel", "channel", "list_channels", lambda info: True, _link_channel, _unlink_channel),
    "category": _KindAdapter(
        "category", "Category", "list_categories", lambda info: info.supports_categories, _link_category, _unlink_category
    ),
    "role": _KindAdapter("role", "role", "list_roles", lambda info: info.supports_roles, _link_role, _unlink_role),
    "user": _KindAdapter("user", "user", "list_users", lambda info: True, _link_user, _unlink_user),
    "emote": _KindAdapter("emote", "emote", "list_emotes", lambda info: info.supports_emotes, _link_emote, _unlink_emote),
}


async def _list_entities(info: "ConnectorInfo", adapter: _KindAdapter) -> list[tuple[str, str]]:
    """Best-effort `list_<kind>` read for `info`, via the adapter's hook
    name - None/missing hook, an exception, or a falsy return all mean "no
    suggestions", same contract as the autocomplete callers of this hook
    family (`_entity_autocomplete_choices`)."""
    hook = getattr(info, adapter.list_hook_name, None)
    if hook is None:
        return []
    try:
        return list(await hook()) or []
    except Exception:
        logger.debug("link editor: %s() failed on %s", adapter.list_hook_name, info.id, exc_info=True)
        return []


class LinkEditorView(discord.ui.View):
    """The live control panel. Construct via `LinkEditorView.create(...)`
    (async - it needs a `describe_group` round-trip to seed itself), not the
    constructor directly."""

    def __init__(self, spec: LinkEditorSpec, *, invoker_id: int, content: str) -> None:
        super().__init__(timeout=_TIMEOUT_SECONDS)
        self.spec = spec
        self.adapter = _ADAPTERS[spec.kind]
        self.invoker_id = invoker_id
        self.content = content
        self.edited_connector = spec.edited_connector
        self.pending_connector = spec.edited_connector
        # Set by the caller once the message carrying this view is known
        # (`interaction.original_response()` / the followup send's return
        # value) - used only by on_timeout, which has no interaction of its
        # own to respond through.
        self.message: discord.Message | None = None

    @classmethod
    async def create(cls, spec: LinkEditorSpec, *, invoker_id: int, content: str) -> "LinkEditorView | None":
        """Build a view seeded from the group's current state, or None if
        `spec`'s edge doesn't actually exist (shouldn't happen right after a
        successful link, but defends against a race)."""
        view = cls(spec, invoker_id=invoker_id, content=content)
        ok = await view._refresh_items()
        return view if ok else None

    # ---------------------------------------------------------------- rendering

    async def _refresh_items(self) -> bool:
        """Rebuild this view's items from a fresh `describe_group` read.
        Returns False (and leaves the view with no items - a terminal panel)
        if the edited edge no longer exists."""
        self.clear_items()
        described = await self.spec.linker.describe_group(
            local_connector=self.spec.local_connector, local_id=self.spec.local_id
        )
        if described is None:
            self.stop()
            return False
        _group_id, members = described
        member = next((m for m in members if m.connector_id == self.edited_connector), None)
        if member is None:
            self.stop()
            return False

        connectors = self.spec.linker.connectors
        connector_options = [
            (cid, info.label)
            for cid, info in connectors.items()
            if cid != self.spec.local_connector and self.adapter.connector_supported(info)
        ]
        if connector_options:
            self.add_item(_ConnectorSelect(self, connector_options))

        counterpart_info = connectors.get(self.pending_connector)
        entities = await _list_entities(counterpart_info, self.adapter) if counterpart_info is not None else []
        default_id = member.entity_id if self.pending_connector == self.edited_connector else None
        self.add_item(_CounterpartSelect(self, entities, default_id))

        self.add_item(_UnlinkButton(self))
        return True

    async def _rerender(self, interaction: discord.Interaction, *, content: str | None = None) -> None:
        if content is not None:
            self.content = content
        ok = await self._refresh_items()
        if not ok:
            self.content = content or "This link no longer exists."
        await interaction.response.edit_message(content=self.content, view=self)

    # ---------------------------------------------------------------- actions

    async def apply_retarget(self, interaction: discord.Interaction, entity_token: str) -> None:
        if not await self._authorize(interaction):
            return
        entity_token = entity_token.strip()
        if not entity_token:
            await interaction.response.send_message("That doesn't look like a valid id or name.", ephemeral=True)
            return
        # Drop the old edge first, then (re)link to the new one - the same
        # "unlink, then link" two commands an operator would run by hand
        # (issue #115's answer to its own open question #2), so a
        # same-connector counterpart change doesn't collide with the
        # still-linked old entity. Not transactional: if the unlink
        # succeeds (or the edge was already gone - best-effort, suppressed)
        # but the link then fails (e.g. the new target already belongs to a
        # different bridge group), the old edge stays dropped rather than
        # being restored - exactly what running the two commands separately
        # would leave behind too.
        with contextlib.suppress(LinkError):
            await self.adapter.unlink(
                self.spec.linker,
                local_connector=self.spec.local_connector,
                local_id=self.spec.local_id,
                destination=self.edited_connector,
            )
        try:
            summary = await self.adapter.link(
                self.spec.linker,
                local_connector=self.spec.local_connector,
                local_id=self.spec.local_id,
                local_name=self.spec.local_name,
                source=self.pending_connector,
                source_id=entity_token,
            )
        except LinkError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        self.edited_connector = self.pending_connector
        await self._rerender(interaction, content=summary)

    async def apply_unlink(self, interaction: discord.Interaction) -> None:
        if not await self._authorize(interaction):
            return
        try:
            summary = await self.adapter.unlink(
                self.spec.linker,
                local_connector=self.spec.local_connector,
                local_id=self.spec.local_id,
                destination=self.edited_connector,
            )
        except LinkError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        self.clear_items()
        self.stop()
        self.content = summary
        await interaction.response.edit_message(content=self.content, view=self)

    async def change_pending_connector(self, interaction: discord.Interaction, connector_id: str) -> None:
        if not await self._authorize(interaction):
            return
        self.pending_connector = connector_id
        await self._rerender(interaction)

    # ---------------------------------------------------------------- auth/lifecycle

    async def _authorize(self, interaction: discord.Interaction) -> bool:
        """Only the operator who ran the launching `/link`/`/mirror`/`/linked`
        command may drive this panel - ephemeral replies already restrict
        *visibility* to them, but a component interaction is a fresh
        interaction the SDK doesn't gate on that by itself. The Manage
        Server recheck is defense-in-depth against that permission having
        been revoked since the command was run; like every other
        "can we tell?" guard in this codebase (can_view_channel,
        is_forum_channel, ...), an interaction.user we can't read
        guild_permissions off (never expected in practice - this view is
        only ever attached to a guild-only slash command's reply) is let
        through rather than blocked on a signal we don't trust."""
        if interaction.user.id != self.invoker_id:
            await interaction.response.send_message(
                "Only the person who ran this command can use these controls.", ephemeral=True
            )
            return False
        permissions = getattr(interaction.user, "guild_permissions", None)
        if permissions is not None and not permissions.manage_guild:
            await interaction.response.send_message(
                "You need Manage Server to use these controls.", ephemeral=True
            )
            return False
        return True

    async def on_timeout(self) -> None:
        for item in self.children:
            item.disabled = True
        if self.message is not None:
            with contextlib.suppress(discord.HTTPException):
                await self.message.edit(view=self)


class _ConnectorSelect(discord.ui.Select):
    """Which connector this edge is linked to. Changing it only updates
    `pending_connector` and re-renders the counterpart list for that
    connector - it doesn't commit anything by itself, since a connector with
    no counterpart chosen yet isn't a valid link."""

    def __init__(self, view: LinkEditorView, options: list[tuple[str, str]]) -> None:
        select_options = [
            discord.SelectOption(label=label[:100], value=cid, default=(cid == view.pending_connector))
            for cid, label in options[:_MAX_SELECT_OPTIONS]
        ]
        super().__init__(placeholder="Linked connector...", options=select_options, min_values=1, max_values=1)
        self._view_ref = view

    async def callback(self, interaction: discord.Interaction) -> None:
        await self._view_ref.change_pending_connector(interaction, self.values[0])


class _CounterpartSelect(discord.ui.Select):
    """Which entity, on the connector `LinkEditorView.pending_connector`
    currently names, this edge is linked to. A trailing "Enter an id/name..."
    option opens a modal instead - the escape hatch for a connector with no
    `list_<kind>` hook, an empty result, or more entities than fit in a
    Select (issue #115's answer to its own open question #5)."""

    def __init__(self, view: LinkEditorView, entities: list[tuple[str, str]], default_id: str | None) -> None:
        capped = entities[: _MAX_SELECT_OPTIONS - 1]
        select_options = [
            discord.SelectOption(label=name[:100] or entity_id, value=entity_id, default=(entity_id == default_id))
            for entity_id, name in capped
        ]
        select_options.append(
            discord.SelectOption(label="Enter an id/name...", value=_ENTER_MANUALLY, default=False)
        )
        super().__init__(placeholder="Linked entity...", options=select_options, min_values=1, max_values=1)
        self._view_ref = view

    async def callback(self, interaction: discord.Interaction) -> None:
        if not await self._view_ref._authorize(interaction):
            return
        if self.values[0] == _ENTER_MANUALLY:
            await interaction.response.send_modal(_CounterpartModal(self._view_ref))
            return
        await self._view_ref.apply_retarget(interaction, self.values[0])


class _CounterpartModal(discord.ui.Modal):
    """The counterpart-select escape hatch: a free-text id/name, resolved
    the same way a typed `/link`/`/mirror` argument is (each linker's own
    `_resolve_to_id`, run inside `adapter.link`)."""

    def __init__(self, view: LinkEditorView) -> None:
        super().__init__(title=f"Set the linked {view.adapter.noun}")
        self._view_ref = view
        self.value = discord.ui.TextInput(label="Id or name", required=True, max_length=200)
        self.add_item(self.value)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self._view_ref.apply_retarget(interaction, str(self.value.value))


class _UnlinkButton(discord.ui.Button):
    def __init__(self, view: LinkEditorView) -> None:
        super().__init__(label="Unlink", style=discord.ButtonStyle.danger)
        self._view_ref = view

    async def callback(self, interaction: discord.Interaction) -> None:
        await self._view_ref.apply_unlink(interaction)
