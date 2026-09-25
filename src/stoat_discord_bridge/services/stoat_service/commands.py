"""Command parsing for the Stoat connector.

Stoat's admin commands are real `stoat.ext.commands` groups (`/link channel
…` etc.), the Stoat analogue of the Discord side's `app_commands` tree.
`build_command_tree` declares the `/link`, `/unlink`, `/linked`, `/mirror`
groups and their subcommands (plus flat `/status`, `/bridge-help`) on a
`_StoatClient`; every callback forwards to the matching
`StoatSenderService._<verb>_<noun>` method in `linking.py`.

`/bridge-help` stays under that name rather than `/help` - a bare `/help`
would collide with other bots' command providers in a shared Stoat server -
but renders from the same `admin_commands.help.HELP_TOPICS` table Discord's
`/help` and IRC's `HELP` do (issue #117), rather than its own hand-maintained
text blob.
"""

from __future__ import annotations

import typing

from stoat_discord_bridge.admin_commands import pop_kv_option, render_help, resolve_help_key
from stoat_discord_bridge.services.stoat_service._compat import apply_stoat_command_patches

# stoat.py 1.2.1's command framework raises `TypeError` on any `Optional[...]`
# parameter (issue #40); patch that before the tree below is declared/invoked.
apply_stoat_command_patches()


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
    # copy. Both lead with a required `<service>` (`all` is a valid explicit
    # value on `to`, but no longer the default on omission - issue #97); a `to`
    # with no local id is a friendly error for role/emote (no "current" one).
    @mirror.group(name="channel", invoke_without_command=True)
    async def mirror_channel(ctx):
        await owner._reply(ctx, f"Usage: {p}mirror channel <to|from> …")

    # Optional params are named `pname:value` tokens (issue #167), using the
    # same names as Discord's options, and may appear anywhere after the
    # subcommand. The leading positionals stay declared so stoat.py still
    # reports a missing `<service>` itself (issue #97); everything else lands
    # in `*options`. The positional count is rechecked after the named tokens
    # are pulled out, since one of them may have been bound to a positional
    # slot - and so a bare trailing name (the old positional `new_name`) gets
    # a usage reply instead of being quietly ignored.
    @mirror_channel.command(name="to")
    async def mirror_channel_to(ctx, service: str, local_id: typing.Optional[str] = None, *options: str):
        tokens = [t for t in (service, local_id, *options) if t is not None]
        tokens, new_name = pop_kv_option(tokens, "new_name")
        tokens, category = pop_kv_option(tokens, "category")
        tokens, history = pop_kv_option(tokens, "history", bare=True)
        if not 1 <= len(tokens) <= 2:
            await owner._reply(
                ctx,
                f"Usage: {p}mirror channel to <service|all> [local_id|name] [new_name:<name>] "
                "[category:<id|name>] [history[:<n|all>]]",
            )
            return
        local_id = tokens[1] if len(tokens) > 1 else None
        await owner._mirror_channel(
            ctx, tokens[0], local_id, new_name, category, history is not None, history or None
        )

    @mirror_channel.command(name="from")
    async def mirror_channel_from(
        ctx,
        service: typing.Optional[str] = None,
        external_id: typing.Optional[str] = None,
        *options: str,
    ):
        tokens = [t for t in (service, external_id, *options) if t is not None]
        tokens, new_name = pop_kv_option(tokens, "new_name")
        tokens, category = pop_kv_option(tokens, "category")
        tokens, history = pop_kv_option(tokens, "history", bare=True)
        if len(tokens) != 2:
            await owner._reply(
                ctx,
                f"Usage: {p}mirror channel from <service> <external_id|name> [new_name:<name>] "
                "[category:<local_id|name>] [history[:<n|all>]]",
            )
            return
        await owner._mirror_channel_from(
            ctx, tokens[0], tokens[1], new_name, category, history is not None, history or None
        )

    async def pop_new_name(ctx, tokens: list, min_args: int, max_args: int, usage: str):
        """Pull `new_name:<value>` out of a role/category/emote mirror's
        tokens. Returns `(positionals, new_name)`, or None after replying
        with `usage` if the positional count is off."""
        tokens, new_name = pop_kv_option([t for t in tokens if t is not None], "new_name")
        if not min_args <= len(tokens) <= max_args:
            await owner._reply(ctx, f"Usage: {p}{usage} [new_name:<name>]")
            return None
        return tokens + [None] * (max_args - len(tokens)), new_name

    @mirror.group(name="role", invoke_without_command=True)
    async def mirror_role(ctx):
        await owner._reply(ctx, f"Usage: {p}mirror role <to|from> …")

    @mirror_role.command(name="to")
    async def mirror_role_to(ctx, service: str, local_id: str, *options: str):
        parsed = await pop_new_name(
            ctx, [service, local_id, *options], 2, 2, "mirror role to <service|all> <local_id|name>"
        )
        if parsed:
            (service, local_id), new_name = parsed
            await owner._mirror_role(ctx, service, local_id, new_name)

    @mirror_role.command(name="from")
    async def mirror_role_from(ctx, service: str, external_id: str, *options: str):
        parsed = await pop_new_name(
            ctx, [service, external_id, *options], 2, 2, "mirror role from <service> <external_id|name>"
        )
        if parsed:
            (service, external_id), new_name = parsed
            await owner._mirror_role_from(ctx, service, external_id, new_name)

    @mirror.group(name="category", invoke_without_command=True)
    async def mirror_category(ctx):
        await owner._reply(ctx, f"Usage: {p}mirror category <to|from> …")

    @mirror_category.command(name="to")
    async def mirror_category_to(ctx, service: str, local_id: typing.Optional[str] = None, *options: str):
        parsed = await pop_new_name(
            ctx, [service, local_id, *options], 1, 2, "mirror category to <service|all> [local_id|name]"
        )
        if parsed:
            (service, local_id), new_name = parsed
            await owner._mirror_category(ctx, service, local_id, new_name)

    @mirror_category.command(name="from")
    async def mirror_category_from(ctx, service: str, external_id: str, *options: str):
        parsed = await pop_new_name(
            ctx, [service, external_id, *options], 2, 2, "mirror category from <service> <external_id|name>"
        )
        if parsed:
            (service, external_id), new_name = parsed
            await owner._mirror_category_from(ctx, service, external_id, new_name)

    @mirror.group(name="emote", invoke_without_command=True)
    async def mirror_emote(ctx):
        await owner._reply(ctx, f"Usage: {p}mirror emote <to|from> …")

    @mirror_emote.command(name="to")
    async def mirror_emote_to(ctx, service: str, local_id: str, *options: str):
        parsed = await pop_new_name(
            ctx, [service, local_id, *options], 2, 2, "mirror emote to <service|all> <local_id|name>"
        )
        if parsed:
            (service, local_id), new_name = parsed
            await owner._mirror_emote(ctx, service, local_id, new_name)

    @mirror_emote.command(name="from")
    async def mirror_emote_from(ctx, service: str, external_id: str, *options: str):
        parsed = await pop_new_name(
            ctx, [service, external_id, *options], 2, 2, "mirror emote from <service> <external_id|name>"
        )
        if parsed:
            (service, external_id), new_name = parsed
            await owner._mirror_emote_from(ctx, service, external_id, new_name)

    @bot.command(name="status")
    async def status(ctx):
        await owner._reply(ctx, owner._health.render())

    @bot.command(name="whitelist")
    async def whitelist(ctx, *args: str):
        if not args:
            await owner._reply(ctx, f"Usage: {p}whitelist [add|remove] [local|<service>] <bot_id|name>")
            return
        tokens = list(args)
        action = "add"
        if tokens[0].lower() in ("add", "remove"):
            action = tokens.pop(0).lower()
        target = "local"
        known_connectors = owner._bot_whitelist.connectors if owner._bot_whitelist is not None else {}
        if tokens and (tokens[0].lower() == "local" or tokens[0] in known_connectors):
            target = tokens.pop(0)
        if not tokens:
            await owner._reply(ctx, f"Usage: {p}whitelist [add|remove] [local|<service>] <bot_id|name>")
            return
        await owner._whitelist(ctx, action, target, " ".join(tokens))

    # `/import` / `/export` (issue #161) - flat like /whitelist, since a
    # history transfer creates no link. `limit:<n|all>` is a kv token so it
    # can sit anywhere, like /mirror channel's `category:`.
    def transfer_command(direction: str) -> None:
        @bot.command(name=direction)
        async def command(ctx, *args: str):
            tokens, limit = pop_kv_option(list(args), "limit")
            if len(tokens) < 2:
                await owner._reply(
                    ctx, f"Usage: {p}{direction} <service> <external_channel> [local_channel] [limit:<n|all>]"
                )
                return
            local_channel = tokens[2] if len(tokens) > 2 else None
            await owner._transfer_history(ctx, direction, tokens[0], tokens[1], local_channel, limit or None)

    transfer_command("import")
    transfer_command("export")

    @bot.command(name="whitelisted")
    async def whitelisted(ctx, service: typing.Optional[str] = None):
        await owner._whitelisted(ctx, service or "local")

    # `/attachments` (issue #164) - which platform rebuilds a link's preview.
    @bot.group(name="attachments", invoke_without_command=True)
    async def attachments(ctx):
        await owner._reply(
            ctx, f"Usage: {p}attachments <prefer <discord|stoat> <url-substr>|unprefer <url-substr>|preferences>"
        )

    @attachments.command(name="prefer")
    async def attachments_prefer(ctx, kind: str, url_substring: str):
        await owner._attachments_prefer(ctx, kind, url_substring)

    @attachments.command(name="unprefer")
    async def attachments_unprefer(ctx, url_substring: str):
        await owner._attachments_unprefer(ctx, url_substring)

    @attachments.command(name="preferences")
    async def attachments_preferences(ctx):
        await owner._attachments_preferences(ctx)

    @bot.command(name="bridge-help")
    async def bridge_help(
        ctx, topic: typing.Optional[str] = None, noun: typing.Optional[str] = None
    ):
        await owner._reply(ctx, render_help(resolve_help_key(topic, noun), connector="stoat", prefix=p))
