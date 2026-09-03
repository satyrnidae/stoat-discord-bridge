"""Command parsing for the Stoat connector.

Stoat's admin commands are real `stoat.ext.commands` groups (`/link channel
…` etc.), the Stoat analogue of the Discord side's `app_commands` tree.
`build_command_tree` declares the `/link`, `/unlink`, `/linked`, `/mirror`
groups and their subcommands (plus flat `/status`, `/bridge-help`) on a
`_StoatClient`; every callback forwards to the matching
`StoatSenderService._<verb>_<noun>` method in `linking.py`.
"""

from __future__ import annotations

import typing

from stoat_discord_bridge.admin_commands import pop_kv_option
from stoat_discord_bridge.services.stoat_service._compat import apply_stoat_command_patches

# stoat.py 1.2.1's command framework raises `TypeError` on any `Optional[...]`
# parameter (issue #40); patch that before the tree below is declared/invoked.
apply_stoat_command_patches()

# Discord has native slash-command discoverability; Stoat's commands are
# plain chat messages with no such affordance, hence /bridge-help. See
# COMMANDS.md for full per-command detail - this is a compact pointer to it.
# `{p}` is filled with the connector's configured command prefix
# (`StoatConnectorConfig.command_prefix`, "/" by default) - see `_help_text`.
_HELP_TEXT_TEMPLATE = """Bridge commands (see COMMANDS.md for full detail):
  {p}status - sync target health, read-only
  {p}linked channels [local_id|name] - channels bridged to a channel (default: this one), read-only
  {p}linked users [local_id|name] - cross-connector user links, read-only
  {p}linked roles [local_id|name] - roles linked across the bridge, read-only
  {p}linked categories [local_id|name] - Categories bridged to this channel's Category, read-only
  {p}linked emotes [local_id|name] - custom emoji linked across the bridge, read-only
  {p}link channel [local_id|name] <service> <external_id|name> - bridge a channel (Manage Server)
  {p}link user <service> <external_id|name> <local_id|name> - link a user for mentions/masquerading (Manage Server)
  {p}link emote <service> <external_id|name> <local_id|name> - link a custom emoji (Manage Server)
  {p}link role <local_id|name> <service> <external_id|name> - link a role across connectors (Manage Server)
  {p}link category <service> <external_id|name> [local_id|name] - bridge a Category; new channels in either sync automatically (Manage Server)
  {p}mirror channel to [service|all] [local_id|name] [new_name] [category:<id|name>] | from <service> <external_id|name> [new_name] [category:<id|name>] - create+link a matching channel; category:<> overrides linked Categories (Manage Server)
  {p}mirror role to [service|all] <local_id|name> [new_name] | from <service> <external_id|name> [new_name] - create+link a matching role (Manage Server)
  {p}mirror emote to [service|all] <local_id|name> [new_name] | from <service> <external_id|name> [new_name] - recreate+link a custom emoji (Manage Server)
  {p}mirror category to [service|all] [local_id|name] [new_name] | from <service> <external_id|name> [new_name] - create+link a Category and mirror its channels (Manage Server)
  {p}unlink channel [local_id|name] [service|all] - unlink a channel (default: this one) from one connector, or the whole group (Manage Server)
  {p}unlink user [service|all] [local_id|name] - unlink a user (default: yourself) from one connector, or the whole group (Manage Server)
  {p}unlink role <local_id|name> [service|all] - unlink a role from one connector, or the whole group (Manage Server)
  {p}unlink emote <local_id|name> [service|all] - unlink a custom emoji from one connector, or the whole group (Manage Server)
  {p}unlink category [local_id|name] [service|all] - unlink a Category (default: this channel's) from one connector, or the whole group (Manage Server)
  {p}bridge-help - this message"""


def _help_text(prefix: str) -> str:
    return _HELP_TEXT_TEMPLATE.format(p=prefix)


def build_command_tree(bot, owner, prefix: str) -> None:
    """Declares the `/link`, `/unlink`, `/linked`, `/mirror` groups (+ their
    subcommands) and the flat `/status`, `/bridge-help` commands on `bot`,
    mirroring the Discord `app_commands` tree
    (`discord_service._DiscordClient`). Every callback just forwards to the
    matching `StoatSenderService._<verb>_<noun>` method, which holds the
    shared linking logic and the Manage-Server gate."""
    p = prefix

    @bot.group(name="link", invoke_without_command=True)
    async def link(ctx):
        await owner._reply(ctx, f"Usage: {p}link <channel|role|user|category|emote> …")

    @bot.group(name="unlink", invoke_without_command=True)
    async def unlink(ctx):
        await owner._reply(ctx, f"Usage: {p}unlink <channel|role|user|category|emote> …")

    @bot.group(name="linked", invoke_without_command=True)
    async def linked(ctx):
        await owner._reply(ctx, f"Usage: {p}linked <channels|roles|users|categories|emotes> …")

    @bot.group(name="mirror", invoke_without_command=True)
    async def mirror(ctx):
        await owner._reply(ctx, f"Usage: {p}mirror <channel|role|category|emote> …")

    @link.command(name="channel")
    async def link_channel(ctx, service: str, external_id: str, local_id: typing.Optional[str] = None):
        await owner._link_channel(ctx, service, external_id, local_id)

    @link.command(name="role")
    async def link_role(ctx, local_id: str, service: str, external_id: str):
        await owner._link_role(ctx, local_id, service, external_id)

    @link.command(name="user")
    async def link_user(ctx, service: str, external_id: str, local_id: str):
        await owner._link_user(ctx, service, external_id, local_id)

    @link.command(name="category")
    async def link_category(ctx, service: str, external_id: str, local_id: typing.Optional[str] = None):
        await owner._link_category(ctx, service, external_id, local_id)

    @link.command(name="emote")
    async def link_emote(ctx, service: str, external_id: str, local_id: str):
        await owner._link_emote(ctx, service, external_id, local_id)

    @unlink.command(name="channel")
    async def unlink_channel(ctx, local_id: typing.Optional[str] = None, service: typing.Optional[str] = None):
        await owner._unlink_channel(ctx, local_id, service)

    @unlink.command(name="role")
    async def unlink_role(ctx, local_id: str, service: typing.Optional[str] = None):
        await owner._unlink_role(ctx, local_id, service)

    @unlink.command(name="user")
    async def unlink_user(ctx, service: typing.Optional[str] = None, local_id: typing.Optional[str] = None):
        await owner._unlink_user(ctx, service, local_id)

    @unlink.command(name="category")
    async def unlink_category(ctx, local_id: typing.Optional[str] = None, service: typing.Optional[str] = None):
        await owner._unlink_category(ctx, local_id, service)

    @unlink.command(name="emote")
    async def unlink_emote(ctx, local_id: str, service: typing.Optional[str] = None):
        await owner._unlink_emote(ctx, local_id, service)

    @linked.command(name="channels")
    async def linked_channels(ctx, local_id: typing.Optional[str] = None):
        await owner._linked_channels(ctx, local_id)

    @linked.command(name="roles")
    async def linked_roles(ctx, local_id: typing.Optional[str] = None):
        await owner._linked_roles(ctx, local_id)

    @linked.command(name="users")
    async def linked_users(ctx, local_id: typing.Optional[str] = None):
        await owner._linked_users(ctx, local_id)

    @linked.command(name="categories")
    async def linked_categories(ctx, local_id: typing.Optional[str] = None):
        await owner._linked_categories(ctx, local_id)

    @linked.command(name="emotes")
    async def linked_emotes(ctx, local_id: typing.Optional[str] = None):
        await owner._linked_emotes(ctx, local_id)

    # `/mirror <noun>` is a two-way group: `to` pushes a local entity onto
    # another connector, `from` pulls a remote entity in and creates the local
    # copy. Both lead with `<service>` (defaulting to `all` on `to`); a `to`
    # with no local id is a friendly error for role/emote (no "current" one).
    @mirror.group(name="channel", invoke_without_command=True)
    async def mirror_channel(ctx):
        await owner._reply(ctx, f"Usage: {p}mirror channel <to|from> …")

    # `category:<id|name>` (issue #75) can't be positional - it holds arbitrary
    # ids/names - so it's a `PARAM:value` pair pulled out of the token list
    # anywhere, matching the IRC side. The remaining tokens parse positionally
    # as before. The extra optional slot is just capacity for that kv token.
    @mirror_channel.command(name="to")
    async def mirror_channel_to(
        ctx,
        service: typing.Optional[str] = None,
        local_id: typing.Optional[str] = None,
        new_name: typing.Optional[str] = None,
        category: typing.Optional[str] = None,
    ):
        tokens, category_value = pop_kv_option(
            [t for t in (service, local_id, new_name, category) if t is not None], "category"
        )
        service = tokens[0] if tokens else None
        local_id = tokens[1] if len(tokens) > 1 else None
        new_name = tokens[2] if len(tokens) > 2 else None
        await owner._mirror_channel(ctx, local_id, service, new_name, category_value)

    @mirror_channel.command(name="from")
    async def mirror_channel_from(
        ctx,
        service: typing.Optional[str] = None,
        external_id: typing.Optional[str] = None,
        new_name: typing.Optional[str] = None,
        category: typing.Optional[str] = None,
    ):
        tokens, category_value = pop_kv_option(
            [t for t in (service, external_id, new_name, category) if t is not None], "category"
        )
        if len(tokens) < 2:
            await owner._reply(
                ctx,
                f"Usage: {p}mirror channel from <service> <external_id|name> [new_name] [category:<local_id|name>]",
            )
            return
        new_name = tokens[2] if len(tokens) > 2 else None
        await owner._mirror_channel_from(ctx, tokens[0], tokens[1], new_name, category_value)

    @mirror.group(name="role", invoke_without_command=True)
    async def mirror_role(ctx):
        await owner._reply(ctx, f"Usage: {p}mirror role <to|from> …")

    @mirror_role.command(name="to")
    async def mirror_role_to(
        ctx,
        first: typing.Optional[str] = None,
        second: typing.Optional[str] = None,
        third: typing.Optional[str] = None,
    ):
        # `to <local_id>` or `to <service|all> <local_id> [new_name]` - role has
        # no "current" id to default, so a lone arg is the role, not the service.
        local_id, service = (second, first) if second is not None else (first, None)
        await owner._mirror_role(ctx, local_id, service, third)

    @mirror_role.command(name="from")
    async def mirror_role_from(ctx, service: str, external_id: str, new_name: typing.Optional[str] = None):
        await owner._mirror_role_from(ctx, service, external_id, new_name)

    @mirror.group(name="category", invoke_without_command=True)
    async def mirror_category(ctx):
        await owner._reply(ctx, f"Usage: {p}mirror category <to|from> …")

    @mirror_category.command(name="to")
    async def mirror_category_to(
        ctx,
        service: typing.Optional[str] = None,
        local_id: typing.Optional[str] = None,
        new_name: typing.Optional[str] = None,
    ):
        await owner._mirror_category(ctx, local_id, service, new_name)

    @mirror_category.command(name="from")
    async def mirror_category_from(ctx, service: str, external_id: str, new_name: typing.Optional[str] = None):
        await owner._mirror_category_from(ctx, service, external_id, new_name)

    @mirror.group(name="emote", invoke_without_command=True)
    async def mirror_emote(ctx):
        await owner._reply(ctx, f"Usage: {p}mirror emote <to|from> …")

    @mirror_emote.command(name="to")
    async def mirror_emote_to(
        ctx,
        first: typing.Optional[str] = None,
        second: typing.Optional[str] = None,
        third: typing.Optional[str] = None,
    ):
        # `to <local_id>` or `to <service|all> <local_id> [new_name]` - see mirror_role_to.
        local_id, service = (second, first) if second is not None else (first, None)
        await owner._mirror_emote(ctx, local_id, service, third)

    @mirror_emote.command(name="from")
    async def mirror_emote_from(ctx, service: str, external_id: str, new_name: typing.Optional[str] = None):
        await owner._mirror_emote_from(ctx, service, external_id, new_name)

    @bot.command(name="status")
    async def status(ctx):
        await owner._reply(ctx, owner._health.render())

    @bot.command(name="bridge-help")
    async def bridge_help(ctx):
        await owner._reply(ctx, _help_text(p))
