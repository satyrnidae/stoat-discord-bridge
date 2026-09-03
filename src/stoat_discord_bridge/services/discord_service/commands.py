"""Command parsing for the Discord connector.

Discord's admin commands are real `app_commands` groups (`/link channel …`
etc.). `build_command_tree` declares `/status` plus the `/link`, `/unlink`,
`/linked`, `/mirror` groups and their subcommands on a
`DiscordSenderService`'s `discord.app_commands.CommandTree`; every callback
forwards to the matching `DiscordSenderService._handle_*` method in
`linking.py`. `_connector_autocomplete_choices` is the shared filter behind
every `service` option's autocomplete; `_entity_autocomplete_choices` is the
one behind every `external_id` / `local_id` option's autocomplete (populated
from the target connector's `ConnectorInfo.list_*` hooks).
"""

from __future__ import annotations

import logging
from collections.abc import Callable

import discord
from discord import app_commands

from stoat_discord_bridge.admin_commands import ConnectorInfo

logger = logging.getLogger(__name__)

_CHOICE_LIMIT = 25  # Discord's hard cap on autocomplete results


def _connector_autocomplete_choices(
    current: str,
    connectors: dict[str, ConnectorInfo],
    *,
    include_all: bool = False,
    predicate: Callable[[ConnectorInfo], bool] | None = None,
) -> list[app_commands.Choice[str]]:
    """Shared filtering behind every `service` option's
    autocomplete: Discord expects results ranked/filtered by `current` (the
    option's in-progress text) and caps them at 25 - substring match against
    both the connector id and its display label, so typing either finds it.
    `include_all` adds the literal "all" choice `/mirror channel` accepts.
    `predicate`, if given, drops connectors it returns falsy for - used by the
    role/category/emote subcommands to hide connector kinds (IRC) that have no
    such concept, so `service` never offers an invalid target."""
    current = current.lower()
    choices = [
        app_commands.Choice(name=f"{info.label} ({connector_id})", value=connector_id)
        for connector_id, info in connectors.items()
        if (predicate is None or predicate(info))
        and (current in connector_id.lower() or current in info.label.lower())
    ]
    if include_all and current in "all":
        choices.insert(0, app_commands.Choice(name="all", value="all"))
    return choices[:_CHOICE_LIMIT]


def _entity_autocomplete_choices(
    current: str, entities: list[tuple[str, str]]
) -> list[app_commands.Choice[str]]:
    """Shared filtering behind every `external_id` / `local_id` option's
    autocomplete: takes the `(native_id, name)` pairs a connector's
    `ConnectorInfo.list_*` hook returned, keeps those whose id or name
    contains `current` (case-insensitive), renders each as `"name (id)"`
    (clipped to Discord's 100-char label limit), and caps the list at 25.
    The `value` is always the bare native id - what the linker wants."""
    current = current.lower()
    choices: list[app_commands.Choice[str]] = []
    for entity_id, name in entities:
        entity_id = str(entity_id)
        name = name or ""
        if current and current not in entity_id.lower() and current not in name.lower():
            continue
        label = f"{name} ({entity_id})" if name else entity_id
        choices.append(app_commands.Choice(name=label[:100], value=entity_id[:100]))
        if len(choices) >= _CHOICE_LIMIT:
            break
    return choices


def build_command_tree(service) -> None:
    """Declares `/status` and the `/link`, `/unlink`, `/linked`, `/mirror`
    groups (+ subcommands) on `service.tree`, mirroring the Stoat side's
    `commands.build_command_tree`. Every callback forwards to a
    `DiscordSenderService._handle_<verb>_<noun>` method."""
    self = service

    @self.tree.command(
        name="status", description="Show sync target health (Discord/Stoat/IRC)", guild=self._guild
    )
    async def status_command(interaction: discord.Interaction) -> None:
        await interaction.response.send_message(self._health.render(), ephemeral=True)

    # Channels, roles, users, Categories and emotes all use the `/link
    # <noun>`, `/unlink <noun>`, `/linked <noun>`, `/mirror <noun>`
    # subcommand form (app_commands groups).
    def _linker_service_autocomplete(get_linker, *, include_all: bool, predicate=None):
        async def _ac(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
            linker = get_linker()
            connectors = linker.connectors if linker is not None else {}
            return _connector_autocomplete_choices(
                current, connectors, include_all=include_all, predicate=predicate
            )

        return _ac

    def role_service_autocomplete(*, include_all: bool):
        # IRC has no role concept - never offer it as a `/link role` target.
        return _linker_service_autocomplete(
            lambda: self._role_linker, include_all=include_all, predicate=lambda info: info.supports_roles
        )

    def channel_service_autocomplete(*, include_all: bool):
        return _linker_service_autocomplete(lambda: self._linker, include_all=include_all)

    def user_service_autocomplete(*, include_all: bool):
        return _linker_service_autocomplete(lambda: self._user_linker, include_all=include_all)

    def category_service_autocomplete(*, include_all: bool):
        # IRC has no Category concept - never offer it as a `/link category` target.
        return _linker_service_autocomplete(
            lambda: self._category_linker,
            include_all=include_all,
            predicate=lambda info: info.supports_categories,
        )

    def emote_service_autocomplete(*, include_all: bool):
        # IRC has no custom-emoji concept - never offer it as a `/link emote` target.
        return _linker_service_autocomplete(
            lambda: self._emote_linker, include_all=include_all, predicate=lambda info: info.supports_emotes
        )

    # `external_id` / `local_id` option autocomplete: list the real
    # channels/roles/users/Categories/emoji on a connector (via its
    # `ConnectorInfo.list_*` hooks) so an operator picks one from the menu
    # instead of pasting an id. `external_id` reads the connector chosen in
    # the `service` option (`interaction.namespace.service`); `local_id`
    # always reads this Discord connector. Best-effort - a missing linker,
    # an un-picked `service`, an unset hook, or a raising hook all yield an
    # empty menu, and the option still takes a hand-typed id or name.
    async def _entity_choices(get_linker, connector_id, hook_name: str, current: str):
        linker = get_linker()
        if linker is None or not connector_id:
            return []
        info = linker.connectors.get(connector_id)
        hook = getattr(info, hook_name, None) if info is not None else None
        if hook is None:
            return []
        try:
            entities = await hook()
        except Exception:
            logger.debug(
                "[discord:%s] %s autocomplete lookup failed", self.connector_id, hook_name, exc_info=True
            )
            return []
        return _entity_autocomplete_choices(current, entities)

    def entity_autocomplete(get_linker, hook_name: str):
        """(local_id_autocomplete, external_id_autocomplete) for one entity
        kind - `local_id` off this connector, `external_id` off the one the
        `service` option names."""

        async def _local(interaction: discord.Interaction, current: str):
            return await _entity_choices(get_linker, self.connector_id, hook_name, current)

        async def _external(interaction: discord.Interaction, current: str):
            service = getattr(interaction.namespace, "service", None)
            return await _entity_choices(get_linker, service, hook_name, current)

        return _local, _external

    channel_local_ac, channel_external_ac = entity_autocomplete(lambda: self._linker, "list_channels")
    role_local_ac, role_external_ac = entity_autocomplete(lambda: self._role_linker, "list_roles")
    user_local_ac, user_external_ac = entity_autocomplete(lambda: self._user_linker, "list_users")
    category_local_ac, category_external_ac = entity_autocomplete(
        lambda: self._category_linker, "list_categories"
    )
    emote_local_ac, emote_external_ac = entity_autocomplete(lambda: self._emote_linker, "list_emotes")

    _manage = discord.Permissions(manage_guild=True)
    link_group = app_commands.Group(
        name="link", description="Link an entity across the bridge", default_permissions=_manage
    )
    unlink_group = app_commands.Group(
        name="unlink", description="Unlink an entity's bridge", default_permissions=_manage
    )
    linked_group = app_commands.Group(name="linked", description="List cross-bridge links")
    mirror_group = app_commands.Group(
        name="mirror", description="Create+link a matching entity elsewhere", default_permissions=_manage
    )
    for _g in (link_group, unlink_group, linked_group, mirror_group):
        self.tree.add_command(_g, guild=self._guild)

    @link_group.command(name="role", description="Link a role from another connector to a local role")
    @app_commands.describe(
        local_id="Role id or name on this connector",
        service="Connector id to link from",
        external_id="Role id or name on that connector",
    )
    @app_commands.autocomplete(
        service=role_service_autocomplete(include_all=False),
        local_id=role_local_ac,
        external_id=role_external_ac,
    )
    async def link_role_command(
        interaction: discord.Interaction, local_id: str, service: str, external_id: str
    ) -> None:
        await self._handle_link_role(interaction, local_id, service, external_id)

    @unlink_group.command(name="role", description="Unlink a role - one connector, or the whole group (default: all)")
    @app_commands.describe(
        local_id="Role id or name on this connector",
        service="Connector id to unlink, or 'all' (default: all)",
    )
    @app_commands.autocomplete(service=role_service_autocomplete(include_all=True), local_id=role_local_ac)
    async def unlink_role_command(
        interaction: discord.Interaction, local_id: str, service: str | None = None
    ) -> None:
        await self._handle_unlink_role(interaction, local_id, service)

    @linked_group.command(name="roles", description="List roles linked across the bridge (omit the role to list all)")
    @app_commands.describe(local_id="Role id or name on this connector (omit to list every linked role)")
    @app_commands.autocomplete(local_id=role_local_ac)
    async def linked_roles_command(
        interaction: discord.Interaction, local_id: str | None = None
    ) -> None:
        await self._handle_linked_roles(interaction, local_id, None)

    # `/mirror <noun>` is a two-way group: `to` pushes a local entity onto
    # another connector (the historical `/mirror <noun>` behaviour), `from`
    # pulls a remote entity in and creates the local copy. Every `/mirror`
    # child is a subgroup - no plain subcommands mixed in.
    mirror_role_group = app_commands.Group(
        name="role", description="Mirror a role across connectors and link the two", parent=mirror_group
    )

    @mirror_role_group.command(
        name="to", description="Ensure a linked counterpart of a local role exists on another connector"
    )
    @app_commands.describe(
        service="Connector id to mirror to, or 'all' (default: all)",
        local_id="Role id or name on this connector",
        new_name="Name for the counterpart role on the target connector (default: same as this one)",
    )
    @app_commands.autocomplete(service=role_service_autocomplete(include_all=True), local_id=role_local_ac)
    async def mirror_role_to_command(
        interaction: discord.Interaction,
        service: str | None = None,
        local_id: str | None = None,
        new_name: str | None = None,
    ) -> None:
        await self._handle_mirror_role(interaction, local_id, service, new_name)

    @mirror_role_group.command(
        name="from", description="Create a local role mirroring one from another connector, and link them"
    )
    @app_commands.describe(
        service="Connector id to mirror from",
        external_id="Role id or name on that connector",
        new_name="Name for the new local role (default: same as the source)",
    )
    @app_commands.autocomplete(service=role_service_autocomplete(include_all=False), external_id=role_external_ac)
    async def mirror_role_from_command(
        interaction: discord.Interaction, service: str, external_id: str, new_name: str | None = None
    ) -> None:
        await self._handle_mirror_role_from(interaction, service, external_id, new_name)

    @link_group.command(name="channel", description="Link a channel from another connector to a local channel")
    @app_commands.describe(
        local_id="Channel id or name on this connector (defaults to the current channel)",
        service="Connector id to link from (see /status for configured connectors)",
        external_id="Channel id or name on that connector",
    )
    @app_commands.autocomplete(
        service=channel_service_autocomplete(include_all=False),
        local_id=channel_local_ac,
        external_id=channel_external_ac,
    )
    async def link_channel_command(
        interaction: discord.Interaction, service: str, external_id: str, local_id: str | None = None
    ) -> None:
        await self._handle_link_channel(interaction, service, external_id, local_id)

    @unlink_group.command(
        name="channel", description="Unlink a channel - one connector, or the whole group (default: all)"
    )
    @app_commands.describe(
        local_id="Channel id or name on this connector (defaults to the current channel)",
        service="Connector id to unlink, or 'all' (default: all)",
    )
    @app_commands.autocomplete(
        service=channel_service_autocomplete(include_all=True), local_id=channel_local_ac
    )
    async def unlink_channel_command(
        interaction: discord.Interaction, local_id: str | None = None, service: str | None = None
    ) -> None:
        await self._handle_unlink_channel(interaction, service, local_id)

    @linked_group.command(
        name="channels", description="List channels linked to a given channel (omit it for the current channel)"
    )
    @app_commands.describe(local_id="Channel id or name on this connector (defaults to the current channel)")
    @app_commands.autocomplete(local_id=channel_local_ac)
    async def linked_channels_command(
        interaction: discord.Interaction, local_id: str | None = None
    ) -> None:
        await self._handle_linked_channels(interaction, local_id)

    mirror_channel_group = app_commands.Group(
        name="channel", description="Mirror a channel across connectors and link the two", parent=mirror_group
    )

    @mirror_channel_group.command(
        name="to", description="Ensure a linked counterpart of a local channel exists on another connector"
    )
    @app_commands.describe(
        local_id="Channel id or name on this connector (defaults to the current channel)",
        service="Connector id to mirror to, or 'all' (default: all)",
        new_name="Name for the counterpart channel on the target connector (default: same as this one)",
        category="Category id or name on the target connector to place the counterpart in - overrides linked Categories; requires a single service, not 'all'",
    )
    @app_commands.autocomplete(
        service=channel_service_autocomplete(include_all=True),
        local_id=channel_local_ac,
        category=category_external_ac,
    )
    async def mirror_channel_to_command(
        interaction: discord.Interaction,
        service: str | None = None,
        local_id: str | None = None,
        new_name: str | None = None,
        category: str | None = None,
    ) -> None:
        await self._handle_mirror_channel(interaction, service, local_id, new_name, category)

    @mirror_channel_group.command(
        name="from", description="Create a local channel mirroring one from another connector, and link them"
    )
    @app_commands.describe(
        service="Connector id to mirror from",
        external_id="Channel id or name on that connector",
        new_name="Name for the new local channel (default: same as the source)",
        category="Local Category id or name to place the new channel in - overrides the source channel's linked Category",
    )
    @app_commands.autocomplete(
        service=channel_service_autocomplete(include_all=False),
        external_id=channel_external_ac,
        category=category_local_ac,
    )
    async def mirror_channel_from_command(
        interaction: discord.Interaction,
        service: str,
        external_id: str,
        new_name: str | None = None,
        category: str | None = None,
    ) -> None:
        await self._handle_mirror_channel_from(interaction, service, external_id, new_name, category)

    @link_group.command(
        name="user",
        description="Link a user from another connector to a local member, for mentions and masquerade override",
    )
    @app_commands.describe(
        service="Connector id to link from (see /status for configured connectors)",
        external_id="User id or display name on that connector",
        local_id="The Discord member this is the same person as",
    )
    @app_commands.autocomplete(
        service=user_service_autocomplete(include_all=False), external_id=user_external_ac
    )
    async def link_user_command(
        interaction: discord.Interaction, service: str, external_id: str, local_id: discord.Member
    ) -> None:
        await self._handle_link_user(interaction, service, external_id, local_id)

    @unlink_group.command(
        name="user",
        description="Unlink a user's cross-connector identity - one connector, or the whole group (default: all)",
    )
    @app_commands.describe(
        service="Connector id to unlink, or 'all' to dissolve the whole link group (default: all)",
        local_id="Member to unlink (defaults to yourself)",
    )
    @app_commands.autocomplete(service=user_service_autocomplete(include_all=True))
    async def unlink_user_command(
        interaction: discord.Interaction, service: str | None = None, local_id: discord.Member | None = None
    ) -> None:
        await self._handle_unlink_user(interaction, service, local_id)

    @linked_group.command(
        name="users", description="List cross-connector user links, for debugging - or just one member's, if given"
    )
    @app_commands.describe(local_id="Show only this member's link (omit to list every linked user)")
    async def linked_users_command(
        interaction: discord.Interaction, local_id: discord.Member | None = None
    ) -> None:
        await self._handle_linked_users(interaction, local_id)

    @link_group.command(
        name="category",
        description="Link a Category from another connector; new channels in either side sync automatically",
    )
    @app_commands.describe(
        local_id="Category id or name on this connector (defaults to the current channel's Category)",
        service="Connector id to link from",
        external_id="Category id or name on that connector",
    )
    @app_commands.autocomplete(
        service=category_service_autocomplete(include_all=False),
        local_id=category_local_ac,
        external_id=category_external_ac,
    )
    async def link_category_command(
        interaction: discord.Interaction, service: str, external_id: str, local_id: str | None = None
    ) -> None:
        await self._handle_link_category(interaction, service, external_id, local_id)

    @unlink_group.command(
        name="category", description="Unlink a Category's bridge - one connector, or the whole group (default: all)"
    )
    @app_commands.describe(
        local_id="Category id or name on this connector (defaults to the current channel's Category)",
        service="Connector id to unlink, or 'all' (default: all)",
    )
    @app_commands.autocomplete(
        service=category_service_autocomplete(include_all=True), local_id=category_local_ac
    )
    async def unlink_category_command(
        interaction: discord.Interaction, local_id: str | None = None, service: str | None = None
    ) -> None:
        await self._handle_unlink_category(interaction, local_id, service)

    @linked_group.command(
        name="categories", description="List Categories linked across the bridge (omit to use this channel's)"
    )
    @app_commands.describe(local_id="Category id or name on this connector (defaults to the current channel's)")
    @app_commands.autocomplete(local_id=category_local_ac)
    async def linked_categories_command(
        interaction: discord.Interaction, local_id: str | None = None
    ) -> None:
        await self._handle_linked_categories(interaction, local_id)

    mirror_category_group = app_commands.Group(
        name="category",
        description="Mirror a Category across connectors, link the two, and mirror its channels",
        parent=mirror_group,
    )

    @mirror_category_group.command(
        name="to", description="Ensure a linked counterpart of a local Category exists on another connector"
    )
    @app_commands.describe(
        local_id="Category id or name on this connector (defaults to the current channel's Category)",
        service="Connector id to mirror to, or 'all' (default: all)",
        new_name="Title for the counterpart Category on the target connector (default: same as this one)",
    )
    @app_commands.autocomplete(
        service=category_service_autocomplete(include_all=True), local_id=category_local_ac
    )
    async def mirror_category_to_command(
        interaction: discord.Interaction,
        service: str | None = None,
        local_id: str | None = None,
        new_name: str | None = None,
    ) -> None:
        await self._handle_mirror_category(interaction, local_id, service, new_name)

    @mirror_category_group.command(
        name="from", description="Create a local Category mirroring one from another connector, and link them"
    )
    @app_commands.describe(
        service="Connector id to mirror from",
        external_id="Category id or name on that connector",
        new_name="Title for the new local Category (default: same as the source)",
    )
    @app_commands.autocomplete(
        service=category_service_autocomplete(include_all=False), external_id=category_external_ac
    )
    async def mirror_category_from_command(
        interaction: discord.Interaction, service: str, external_id: str, new_name: str | None = None
    ) -> None:
        await self._handle_mirror_category_from(interaction, service, external_id, new_name)

    @link_group.command(name="emote", description="Link a custom emoji from another connector to a local one")
    @app_commands.describe(
        service="Connector id to link from (see /status for configured connectors)",
        external_id="Emoji id or name on that connector",
        local_id="Emoji id or name on this connector",
    )
    @app_commands.autocomplete(
        service=emote_service_autocomplete(include_all=False),
        local_id=emote_local_ac,
        external_id=emote_external_ac,
    )
    async def link_emote_command(
        interaction: discord.Interaction, service: str, external_id: str, local_id: str
    ) -> None:
        await self._handle_link_emote(interaction, service, external_id, local_id)

    @unlink_group.command(
        name="emote", description="Unlink a custom emoji - one connector, or the whole group (default: all)"
    )
    @app_commands.describe(
        local_id="Emoji id or name on this connector",
        service="Connector id to unlink, or 'all' (default: all)",
    )
    @app_commands.autocomplete(service=emote_service_autocomplete(include_all=True), local_id=emote_local_ac)
    async def unlink_emote_command(
        interaction: discord.Interaction, local_id: str, service: str | None = None
    ) -> None:
        await self._handle_unlink_emote(interaction, local_id, service)

    @linked_group.command(
        name="emotes", description="List custom emoji linked across the bridge (omit the emote to list all)"
    )
    @app_commands.describe(local_id="Emoji id or name on this connector (omit to list every linked emote)")
    @app_commands.autocomplete(local_id=emote_local_ac)
    async def linked_emotes_command(
        interaction: discord.Interaction, local_id: str | None = None
    ) -> None:
        await self._handle_linked_emotes(interaction, local_id)

    mirror_emote_group = app_commands.Group(
        name="emote", description="Mirror a custom emoji across connectors and link the two", parent=mirror_group
    )

    @mirror_emote_group.command(
        name="to", description="Recreate a local custom emoji on another connector and link the two"
    )
    @app_commands.describe(
        service="Connector id to mirror to, or 'all' (default: all)",
        local_id="Emoji id or name on this connector",
        new_name="Name for the counterpart emoji on the target connector (default: same as this one)",
    )
    @app_commands.autocomplete(service=emote_service_autocomplete(include_all=True), local_id=emote_local_ac)
    async def mirror_emote_to_command(
        interaction: discord.Interaction,
        service: str | None = None,
        local_id: str | None = None,
        new_name: str | None = None,
    ) -> None:
        await self._handle_mirror_emote(interaction, local_id, service, new_name)

    @mirror_emote_group.command(
        name="from", description="Recreate a custom emoji from another connector locally and link the two"
    )
    @app_commands.describe(
        service="Connector id to mirror from",
        external_id="Emoji id or name on that connector",
        new_name="Name for the new local emoji (default: same as the source)",
    )
    @app_commands.autocomplete(service=emote_service_autocomplete(include_all=False), external_id=emote_external_ac)
    async def mirror_emote_from_command(
        interaction: discord.Interaction, service: str, external_id: str, new_name: str | None = None
    ) -> None:
        await self._handle_mirror_emote_from(interaction, service, external_id, new_name)
