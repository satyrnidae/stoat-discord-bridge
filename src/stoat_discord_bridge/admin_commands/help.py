"""Shared help content for the bridge's admin/status commands, rendered by
all three connectors (Discord's `/help`, Stoat's `/bridge-help`, IRC's
`HELP`) instead of each maintaining its own hand-written text blob that
drifts from `COMMANDS.md` and from the other two connectors (issue #117).

`HELP_TOPICS` is the single source of truth; `render_help` renders either the
top-level index (every topic this connector offers, one line each) or one
topic's detail, dropping/hiding whatever a connector doesn't support (IRC has
no role/Category/emote concept, and `/mirror` has no `user` noun anywhere).
`COMMANDS.md` stays the canonical prose doc - this is its machine-readable
digest, kept in sync by hand.
"""

from __future__ import annotations

from dataclasses import dataclass

# The three connector kinds a topic's syntax line may be defined for. Not an
# enum, same reasoning as models.ConnectorId - free-form connector ids exist
# per kind, but a help *topic*'s syntax only varies by kind, not by the
# individual configured connector.
_ConnectorKind = str


@dataclass(frozen=True)
class HelpTopic:
    """One entry in `HELP_TOPICS`.

    `summary` is the one-line index description; `body` is the fuller
    drill-down prose. `syntax` is keyed `"discord"` / `"stoat"` / `"irc"` -
    the literal command syntax for Discord and IRC, a `{p}`-templated one for
    Stoat (its `command_prefix` is configurable per connector, see
    `_render_syntax`); `None` means this connector doesn't offer the topic at
    all (dropped from its index, and looking it up falls back to the index -
    see `render_help`). `permission` is a short human-readable gate
    description, e.g. `"Manage Server"`, `"IRC-operator"`, or `"read-only"`.
    """

    summary: str
    body: str
    syntax: dict[_ConnectorKind, str | None]
    permission: str


# Keyed "verb noun" (e.g. "link channel"), or the bare verb for `status` -
# the same shape Discord's single combined `topic` choice value is, and what
# Stoat's `/bridge-help [topic] [noun]` / IRC's `HELP [topic] [noun]`
# positional args combine into via `resolve_help_key`. Order here is the
# order topics are listed in the index.
HELP_TOPICS: dict[str, HelpTopic] = {
    "status": HelpTopic(
        summary="Sync target health per connector, read-only",
        body=(
            "Reports each connector's sync health (healthy/degraded/failing), tracked from "
            "connection state and recent relay outcomes. A GET /status JSON endpoint on the "
            "health-check server (see the Docker section of README.md) mirrors the same data."
        ),
        syntax={"discord": "/status", "stoat": "{p}status", "irc": "STATUS"},
        permission="read-only",
    ),
    "link channel": HelpTopic(
        summary="Bridge a channel to one on another connector",
        body=(
            "Links <external_id> on <service> to <local_id> (or the invoking channel, "
            "Discord/Stoat only - IRC has no \"current channel\" for a DM, so it's always "
            "required there). If either side is already linked, the existing bridge group is "
            "reused; linking two channels that are each already in different groups fails - "
            "unlink one side first. Every id also accepts a bare channel name. Pointing this at "
            "a Discord forum channel behaves like `link category` instead - a forum acts like a "
            "Category, its posts are threads already mirrored as their own channels."
        ),
        syntax={
            "discord": "/link channel <service> <external_id> [local_id]",
            "stoat": "{p}link channel [local_id|name] <service> <external_id|name>",
            "irc": "LINK CHANNEL <local_id> <service> <external_id>",
        },
        permission="Manage Server (Discord/Stoat) / IRC-operator (IRC)",
    ),
    "link user": HelpTopic(
        summary="Link a user's identity across connectors",
        body=(
            "Links <service>'s user to a local identity, for @mention rewriting and so a "
            "relayed message from the remote user masquerades as the linked local identity "
            "instead of showing the remote user's own name/avatar. Every id also accepts a "
            "bare display name/username - not needed on IRC, where a user id already is the "
            "nick. Relinking a connector's id within an existing group replaces the old entry "
            "rather than leaving both on file."
        ),
        syntax={
            "discord": "/link user <service> <external_id> <local_id>",
            "stoat": "{p}link user <service> <external_id|name> <local_id|name>",
            "irc": "LINK USER <service> <external_id|name> <local_id|name>",
        },
        permission="Manage Server (Discord/Stoat) / IRC-operator (IRC)",
    ),
    "link role": HelpTopic(
        summary="Link a role across connectors (Discord/Stoat only)",
        body=(
            "Links <service>'s role to a local role. Once linked, three things happen "
            "automatically: a linked user gaining/losing the role has it granted/revoked on "
            "their linked identity elsewhere (needs Discord's privileged members intent for "
            "the Discord->other direction); renaming a linked role renames every linked copy; "
            "and a linked role's permission override on a bridged channel/category mirrors "
            "onto the linked copy (only the permission bits that mean the same on both "
            "platforms)."
        ),
        syntax={
            "discord": "/link role <local_id> <service> <external_id>",
            "stoat": "{p}link role <local_id|name> <service> <external_id|name>",
            "irc": None,
        },
        permission="Manage Server",
    ),
    "link category": HelpTopic(
        summary="Link a Category across connectors (Discord/Stoat only)",
        body=(
            "Links the invoking channel's Category (or <local_id>'s) to <external_id>'s "
            "Category on <service>. Once two Categories are linked, a new channel created "
            "inside either one is automatically mirrored (created + linked) into every other "
            "connector's own linked Category. A Category Discord's thread/forum-post "
            "auto-mirroring created can never be linked this way."
        ),
        syntax={
            "discord": "/link category <service> <external_id> [local_id]",
            "stoat": "{p}link category <service> <external_id|name> [local_id|name]",
            "irc": None,
        },
        permission="Manage Server",
    ),
    "link emote": HelpTopic(
        summary="Link a custom emoji across connectors (Discord/Stoat only)",
        body=(
            "Links a custom emoji from <service> to a local one, so a reaction using either "
            "can be recreated as the other for reaction sync."
        ),
        syntax={
            "discord": "/link emote <service> <external_id> <local_id>",
            "stoat": "{p}link emote <service> <external_id|name> <local_id|name>",
            "irc": None,
        },
        permission="Manage Server",
    ),
    "linked channels": HelpTopic(
        summary="List channels bridged to a channel, read-only",
        body=(
            "Read-only listing of every channel bridged to <local_id> (or the invoking "
            "channel), across every connector in its bridge group."
        ),
        syntax={
            "discord": "/linked channels [local_id]",
            "stoat": "{p}linked channels [local_id|name]",
            "irc": "LINKED CHANNELS <local_id>",
        },
        permission="read-only",
    ),
    "linked users": HelpTopic(
        summary="List cross-connector user links, read-only",
        body=(
            "Read-only. With no target, lists every user link group; given one, shows just "
            "that identity's group. Each entry's display name is resolved live from its own "
            "connector, not read from storage."
        ),
        syntax={
            "discord": "/linked users [local_id]",
            "stoat": "{p}linked users [local_id|name]",
            "irc": "LINKED USERS [local_id]",
        },
        permission="read-only",
    ),
    "linked roles": HelpTopic(
        summary="List cross-connector role links, read-only (Discord/Stoat only)",
        body=(
            "Read-only. With a role, lists its linked counterparts; with no argument, lists "
            "every linked-role group."
        ),
        syntax={
            "discord": "/linked roles [local_id]",
            "stoat": "{p}linked roles [local_id|name]",
            "irc": None,
        },
        permission="read-only",
    ),
    "linked categories": HelpTopic(
        summary="List cross-connector Category links, read-only (Discord/Stoat only)",
        body=(
            "Read-only listing of every Category linked to the given (or invoking) Category, "
            "across every connector in its bridge group."
        ),
        syntax={
            "discord": "/linked categories [local_id]",
            "stoat": "{p}linked categories [local_id|name]",
            "irc": None,
        },
        permission="read-only",
    ),
    "linked emotes": HelpTopic(
        summary="List cross-connector emoji links, read-only (Discord/Stoat only)",
        body=(
            "Read-only. With an emote, lists its linked counterparts; with no argument, lists "
            "every linked-emote group."
        ),
        syntax={
            "discord": "/linked emotes [local_id]",
            "stoat": "{p}linked emotes [local_id|name]",
            "irc": None,
        },
        permission="read-only",
    ),
    "mirror channel": HelpTopic(
        summary="Create+link a matching channel elsewhere, or pull one in",
        body=(
            "`to` ensures a linked counterpart of a local channel exists on <service> (or "
            "every other connector, if all), creating it via that connector's channel-creation "
            "hook if needed. `from` is the reverse: a remote channel already exists, so a local "
            "counterpart is created and linked. Both take an optional new_name and an optional "
            "Category override; a linked-Category's channels are placed there automatically "
            "without one. Only one /mirror may write into a given destination at a time."
        ),
        syntax={
            "discord": (
                "/mirror channel to <service|all> [local_id] [new_name] [category]"
                " | /mirror channel from <service> <external_id> [new_name] [category]"
            ),
            "stoat": (
                "{p}mirror channel to <service|all> [local_id|name] [new_name] [category:<id|name>]"
                " | {p}mirror channel from <service> <external_id|name> [new_name] [category:<id|name>]"
            ),
            "irc": (
                "MIRROR CHANNEL TO <service|all> <local_id> [AS <new_name>] [CATEGORY:<id|name>]"
                " | MIRROR CHANNEL FROM <service> <external_id> [AS <new_name>]"
            ),
        },
        permission="Manage Server (Discord/Stoat) / IRC-operator (IRC)",
    ),
    "mirror role": HelpTopic(
        summary="Create+link a matching role elsewhere, or pull one in (Discord/Stoat only)",
        body=(
            "`to` ensures a linked counterpart of a local role exists on <service> (or all), "
            "reusing a same-named role there or creating a bare one (name only - color/"
            "permissions aren't copied). `from` is the reverse."
        ),
        syntax={
            "discord": "/mirror role to <service|all> <local_id> [new_name] | /mirror role from <service> <external_id> [new_name]",
            "stoat": (
                "{p}mirror role to <service|all> <local_id|name> [new_name]"
                " | {p}mirror role from <service> <external_id|name> [new_name]"
            ),
            "irc": None,
        },
        permission="Manage Server",
    ),
    "mirror category": HelpTopic(
        summary="Create+link a matching Category and mirror its channels (Discord/Stoat only)",
        body=(
            "`to` ensures a linked counterpart Category exists on <service> (or all), then "
            "relocates the source Category's channels onto it - a child already linked is "
            "moved, an unlinked one is mirrored. `from` is the reverse."
        ),
        syntax={
            "discord": "/mirror category to <service|all> [local_id] [new_name] | /mirror category from <service> <external_id> [new_name]",
            "stoat": (
                "{p}mirror category to <service|all> [local_id|name] [new_name]"
                " | {p}mirror category from <service> <external_id|name> [new_name]"
            ),
            "irc": None,
        },
        permission="Manage Server",
    ),
    "mirror emote": HelpTopic(
        summary="Recreate+link a custom emoji elsewhere, or pull one in (Discord/Stoat only)",
        body=(
            "`to` ensures a linked counterpart of a local emoji exists on <service> (or all): "
            "reuses an existing link, then a same-named emoji, and only then reads the source "
            "image and recreates it. `from` is the reverse. A connector that can't read or "
            "create the emoji (slots full, name rejected, image too large) is reported "
            "per-connector."
        ),
        syntax={
            "discord": "/mirror emote to <service|all> <local_id> [new_name] | /mirror emote from <service> <external_id> [new_name]",
            "stoat": (
                "{p}mirror emote to <service|all> <local_id|name> [new_name]"
                " | {p}mirror emote from <service> <external_id|name> [new_name]"
            ),
            "irc": None,
        },
        permission="Manage Server",
    ),
    "unlink channel": HelpTopic(
        summary="Unlink a channel from one connector, or the whole group",
        body=(
            "Given a service, kicks just that member out - the rest of the group stays linked. "
            "With no argument, or all, dissolves the whole group. A kick that would leave a "
            "single member dissolves the group instead. A channel left with no linked "
            "counterparts is announced to its connector; IRC posts a notice and leaves it. "
            "With all as the channel, does this for every channel this connector has linked - "
            "the service is then required (a connector, or all to dissolve every group)."
        ),
        syntax={
            "discord": "/unlink channel [local_id|all] [service|all]",
            "stoat": "{p}unlink channel [local_id|name|all] [service|all]",
            "irc": "UNLINK CHANNEL <local_id|all> [service|all]",
        },
        permission="Manage Server (Discord/Stoat) / IRC-operator (IRC)",
    ),
    "unlink user": HelpTopic(
        summary="Unlink a user from one connector, or the whole group",
        body=(
            "Given a service, kicks just that identity out - the rest of the group stays "
            "linked. With no service, or all, dissolves the whole group. local_id defaults to "
            "whoever ran the command."
        ),
        syntax={
            "discord": "/unlink user [service] [local_id]",
            "stoat": "{p}unlink user [service|all] [local_id|name]",
            "irc": "UNLINK USER [service|all] [local_id]",
        },
        permission="Manage Server (Discord/Stoat) / IRC-operator (IRC)",
    ),
    "unlink role": HelpTopic(
        summary="Unlink a role from one connector, or the whole group (Discord/Stoat only)",
        body=(
            "Given a service, kicks just that one out; with no argument, or all, dissolves the "
            "whole group. A kick that would leave a single member dissolves the group instead. "
            "The roles themselves are never deleted."
        ),
        syntax={
            "discord": "/unlink role <local_id> [service|all]",
            "stoat": "{p}unlink role <local_id|name> [service|all]",
            "irc": None,
        },
        permission="Manage Server",
    ),
    "unlink category": HelpTopic(
        summary="Unlink a Category from one connector, or the whole group (Discord/Stoat only)",
        body=(
            "Given a service, kicks just that member out; with no argument, or all, dissolves "
            "the whole group. Existing channels already synced into the Category are left "
            "alone either way - only future auto-sync stops."
        ),
        syntax={
            "discord": "/unlink category [local_id] [service|all]",
            "stoat": "{p}unlink category [local_id|name] [service|all]",
            "irc": None,
        },
        permission="Manage Server",
    ),
    "unlink emote": HelpTopic(
        summary="Unlink a custom emoji from one connector, or the whole group (Discord/Stoat only)",
        body=(
            "Given a service, kicks just that one out; with no argument, or all, dissolves the "
            "whole group. A kick that would leave a single member dissolves the group instead. "
            "The emoji themselves are never deleted."
        ),
        syntax={
            "discord": "/unlink emote <local_id> [service|all]",
            "stoat": "{p}unlink emote <local_id|name> [service|all]",
            "irc": None,
        },
        permission="Manage Server",
    ),
    "import": HelpTopic(
        summary="Copy another channel's message history into a channel here",
        body=(
            "Copies the history of <external_channel> on <service> into <local_channel> (or the "
            "invoking channel, Discord/Stoat only - IRC always needs it). Both channels must "
            "already exist; no link is made or needed, and <service> may be this same connector. "
            "Posts the bridge relayed and bot posts are included. Copied messages land only in "
            "<local_channel>, never in the channels it's linked to, and don't sync later edits "
            "or reactions. history_limit works like /mirror channel's: 50 by default, a number up "
            "to 1000, or all. Blocks /mirror into either connector while it runs. Copying from IRC "
            "briefly leaves and rejoins the channel; copying into IRC needs chanhistory (H)."
        ),
        syntax={
            "discord": "/import <service> <external_channel> [local_channel] [history_limit]",
            "stoat": "{p}import <service> <external_channel|name> [local_channel|name] [limit:<n|all>]",
            "irc": "IMPORT <service> <external_channel> <local_channel> [LIMIT:<n|all>]",
        },
        permission="Manage Server (Discord/Stoat) / IRC-operator (IRC)",
    ),
    "export": HelpTopic(
        summary="Copy a channel's message history here into another channel",
        body=(
            "The reverse of import: copies the history of <local_channel> (or the invoking "
            "channel, Discord/Stoat only) into <external_channel> on <service>. Same rules and "
            "limits as import."
        ),
        syntax={
            "discord": "/export <service> <external_channel> [local_channel] [history_limit]",
            "stoat": "{p}export <service> <external_channel|name> [local_channel|name] [limit:<n|all>]",
            "irc": "EXPORT <service> <external_channel> <local_channel> [LIMIT:<n|all>]",
        },
        permission="Manage Server (Discord/Stoat) / IRC-operator (IRC)",
    ),
    "whitelist": HelpTopic(
        summary="Allow (or disallow) a bot's activity to relay (Discord/Stoat only)",
        body=(
            "Bot-authored messages/edits/reactions are dropped by every sender by default - "
            "this manages a per-connector allowlist of bot users whose activity relays like a "
            "human's. action defaults to add; service defaults to local (the connector the "
            "command is run on) - pass an explicit connector id to whitelist a bot that lives "
            "on another one. Also respects link user: whitelisting a bot on one connector "
            "re-admits its linked identity's activity everywhere it's linked. Entries can also "
            "come from a config.yaml whitelisted_bots: seed, which can't be removed here. Never "
            "whitelist another bridge instance's own bot - its relayed copies would loop back."
        ),
        syntax={
            "discord": "/whitelist [action] [service] <bot>",
            "stoat": "{p}whitelist [add|remove] [local|<service>] <bot_id|name>",
            "irc": None,
        },
        permission="Manage Server",
    ),
    "whitelisted": HelpTopic(
        summary="List whitelisted bots on a connector, read-only (Discord/Stoat only)",
        body=(
            "Lists the bots whitelisted on the given connector (default: local, the one the "
            "command is run on), config-seeded entries marked separately from runtime ones."
        ),
        syntax={
            "discord": "/whitelisted [service]",
            "stoat": "{p}whitelisted [local|<service>]",
            "irc": None,
        },
        permission="read-only",
    ),
    # One topic for all three subcommands - Discord's /help dropdown is capped
    # at 25 choices.
    "attachments": HelpTopic(
        summary="Choose which platform builds link previews (Discord/Stoat only)",
        body=(
            "A link preview (Instagram, YouTube, an article...) is relayed as the source "
            "platform's own preview media, re-uploaded as an attachment, with the link removed "
            "from the text. prefer <discord|stoat> <url-substr> makes that platform build its own "
            "preview instead for links containing the text (case-insensitive; the longest match "
            "wins): it gets the plain link and the attachment is skipped. unprefer removes a "
            "rule; preferences lists them. Rules apply to every connector of that kind. IRC has "
            "no previews - it always gets the plain link."
        ),
        syntax={
            "discord": "/attachments prefer <kind> <url_substring> | unprefer <url_substring> | preferences",
            "stoat": "{p}attachments prefer <discord|stoat> <url-substr> | unprefer <url-substr> | preferences",
            "irc": None,
        },
        permission="Manage Server (preferences is read-only on Stoat)",
    ),
}

# This connector's own help command, appended to the index - not itself a
# HELP_TOPICS entry since it takes no drill-down of its own.
_HELP_COMMAND_SYNTAX: dict[_ConnectorKind, str] = {
    "discord": "/help [topic]",
    "stoat": "{p}bridge-help [topic] [noun]",
    "irc": "HELP [topic] [noun]",
}


def resolve_help_key(topic: str | None, noun: str | None = None) -> str:
    """Combine Stoat's `/bridge-help [topic] [noun]` / IRC's `HELP [topic]
    [noun]` positional args into one `HELP_TOPICS` lookup key (e.g. "link
    channel") - the same shape Discord's single combined `topic` choice
    value already is. Case/whitespace-insensitive; a missing part just
    yields fewer tokens (`resolve_help_key("status")` -> `"status"`,
    `resolve_help_key(None)` -> `""`, matching no topic - the index)."""
    parts = [part.strip().lower() for part in (topic, noun) if part and part.strip()]
    return " ".join(parts)


def _render_syntax(template: str | None, *, connector: str, prefix: str) -> str | None:
    if template is None:
        return None
    return template.format(p=prefix) if connector == "stoat" else template


def render_help(topic: str | None, *, connector: str, prefix: str = "/") -> str:
    """Render this connector's help content: the top-level index when
    `topic` is `None`, unrecognized, or not offered on `connector` (IRC
    asking about a role/Category/emote topic, say), otherwise that topic's
    detail. `prefix` only matters for `connector == "stoat"` (its
    configurable `command_prefix`); ignored otherwise. `topic` is expected
    already combined via `resolve_help_key` for Stoat/IRC's two-positional-
    arg form; Discord's single `topic` choice value is used as-is."""
    key = (topic or "").strip().lower()
    entry = HELP_TOPICS.get(key)
    if entry is None or _render_syntax(entry.syntax.get(connector), connector=connector, prefix=prefix) is None:
        return _render_index(connector=connector, prefix=prefix)
    return _render_topic(entry, connector=connector, prefix=prefix)


def _render_index(*, connector: str, prefix: str) -> str:
    lines = ["Bridge commands (see COMMANDS.md for full detail):"]
    for entry in HELP_TOPICS.values():
        syntax = _render_syntax(entry.syntax.get(connector), connector=connector, prefix=prefix)
        if syntax is None:
            continue
        lines.append(f"  {syntax} - {entry.summary}")
    help_syntax = _render_syntax(_HELP_COMMAND_SYNTAX[connector], connector=connector, prefix=prefix)
    lines.append(f"  {help_syntax} - this message")
    return "\n".join(lines)


def _render_topic(entry: HelpTopic, *, connector: str, prefix: str) -> str:
    syntax = _render_syntax(entry.syntax[connector], connector=connector, prefix=prefix)
    return "\n".join([syntax, "", entry.body, "", f"Permission: {entry.permission}"])
