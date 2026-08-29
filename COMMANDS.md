# Commands

Every admin/status command the bridge exposes, and how to reach it on each
connector. Shared logic lives in `src/stoat_discord_bridge/admin_commands.py`
(`ChannelLinker` / `CategoryLinker` / `EmoteLinker` / `UserLinker` /
`RoleLinker` / `StructureMirrorer`); each
connector's own `services/*.py` module just wires its native command syntax
to that shared logic, so behavior is identical everywhere except where noted.

Discord has native slash-command discoverability, so it has no dedicated
help command. Stoat and IRC don't, hence `/bridge-help` (Stoat) and `HELP`
(IRC) - both just print a compact copy of this file's per-connector command
list.

A `<service>` argument below is a connector `id` from
`config.yaml` (see its `id` field) — not a platform name, since there can be
any number of connectors of each kind. On Discord, every such argument has
autocomplete listing the bridge's currently configured connectors.

`<external_id>` is an id that lives on *another* connector; `<local_id>` is an
id on the connector the command is run on.

## Conventions by connector

- **Discord**: slash commands. Anything that changes bridge state requires
  the Manage Server permission; read-only commands (`/status`,
  `/linked-channels`, `/linked users`) don't.
- **Stoat**: message commands (type the command as a plain chat message).
  Same Manage Server / read-only split as Discord.
- **IRC**: sent as a **DM to the bot**, bare and **uppercase**, no leading
  `/` or `!` (unlike Discord/Stoat's slash commands — many IRC clients treat
  a leading `/` as a local client command and never send it as text). Most
  are single underscore-joined tokens (`LINK_CHANNEL`); the user commands are
  two words (`LINK USER` / `UNLINK USER` / `LINKED USERS`), mirroring the
  Discord/Stoat subcommand form. Anything that changes bridge state requires
  **IRC-operator status**,
  checked live via `WHOIS` (not channel-operator status — a DM has no
  per-channel permission to check against in the first place); read-only
  commands need no permission. A DM also has no "current channel" the way a
  Discord/Stoat command run *in* a channel does, so any argument that would
  otherwise default to "the channel this was run in" is always required on
  IRC instead - and, for `MIRROR_CHANNEL`/`UNLINK_CHANNEL`, is hoisted to the
  first position in IRC's syntax since it's the one argument IRC can't let
  slide.

## `/status`

Reports sync target health (`healthy` / `degraded` / `failing`) per
connector, tracked in `status.py`'s `HealthTracker` from each sender's
connection state and recent relay outcomes. No permission gate — read-only.

- **Discord**: `/status` slash command
- **Stoat**: `/status` message command
- **IRC**: `STATUS`, sent as a DM to the bot

A `GET /status` JSON endpoint on the health-check server (see the Docker
section of `README.md`) mirrors the same data.

## `/link-channel <service> <external_id> [<local_id>]`

Links `external_id` on connector `<service>` to `<local_id>` on the
connector the command is run on — or to the current channel if
`<local_id>` is omitted (Discord/Stoat only; IRC has no "current
channel" for a DM, so it's always required there). If either channel is
already linked, the existing bridge group is reused; if *both* are already
linked to two *different* groups, the command fails rather than merging them
(unlink one side first — there's no unlink command yet, so that currently
means linking one side elsewhere to move it into a different group).

- **Discord**: `/link-channel` slash command (Manage Server)
- **Stoat**: `/link-channel <service> <external_id> [<local_id>]` message
  command (Manage Server)
- **IRC**: `LINK_CHANNEL <service> <external_id> <local_id>`, DM (IRC-operator)

## `/linked-channels`

Read-only listing of every channel bridged to the invoking channel, across
every connector in its bridge group.

- **Discord**: `/linked-channels` slash command (defaults to the current channel)
- **Stoat**: `/linked-channels` message command (defaults to the current channel)
- **IRC**: `LINKED_CHANNELS <local_id>`, DM (channel always required)

## Users: `/link user`, `/linked users`, `/unlink user`

Links a user's identity across connectors, for two things: `services/
mentions.py`'s @mention rewriting, and making a message from the linked
remote user masquerade as the local identity when relayed (webhook
username+avatar on Discord, masquerade name+avatar on Stoat, the `<nick>`
prefix on IRC) instead of showing the remote user's own name/avatar. Same
already-linked/conflicting-group rules as `/link-channel`.

These use a **space-separated subcommand** syntax on every connector - on
Discord real `app_commands` subcommand groups (`/link user`, `/unlink user`,
`/linked users`), on Stoat the same tokens as a plain chat message, on IRC an
upper-case `LINK USER` / `UNLINK USER` / `LINKED USERS` DM. Every id argument
also accepts a bare **display name / username** (resolved case-insensitively
against the connector it lives on; first match wins - pass an id when a name
is ambiguous). On IRC no resolution is needed - a user id there already *is*
the nick.

### `/link user <service> <external_id|name> <local_id|name>`

Links `service`'s user to a local identity. Manage Server (Discord/Stoat) /
IRC-operator (IRC).

- **Discord**: `/link user` slash subcommand (Manage Server). The local side
  (`local_id`) is still picked from Discord's own member search — a real
  `discord.Member` option, not typed text. Deliberately: a free-text id
  field here previously let someone type a bare `@name` instead of the real
  snowflake, which silently linked a nonexistent id and broke mention
  rewriting in both directions with no error. The *other* connector's
  `external_id` accepts a name.
- **Stoat**: `/link user <service> <external_id|name> <local_id|name>` message
  command (Manage Server) — both sides accept a name or an id (no
  member-picker equivalent exists there).
- **IRC**: `LINK USER <service> <external_id|name> <local_id|name>`, DM
  (IRC-operator)

Relinking a connector's id within an existing group (e.g. correcting a
mistake) replaces the old entry rather than leaving both on file for mention
rewriting to pick between nondeterministically.

### `/linked users [local_id|name]`

Read-only listing of cross-connector user links, for debugging. With no
target, lists every link group; given one, shows just that identity's group.
Each entry's display name is resolved **live** from its own connector
(`ConnectorInfo.resolve_user_name`) rather than read from storage, since the
stored name is never more than the id it was linked with.

- **Discord**: `/linked users [local_id]` slash subcommand (`local_id` a real `discord.Member`, optional)
- **Stoat**: `/linked users [local_id|name]` message command
- **IRC**: `LINKED USERS [local_id]`, DM

## `/link-emote <service> <external_id> <local_id>`

Links a custom emoji from connector `<service>` to a local custom emoji, so a
reaction using either can be recreated as the other (see the reaction/emoji
sync section of `README.md`/`CLAUDE.md`). Same already-linked/
conflicting-group rules as `/link-channel`.

- **Discord**: `/link-emote` slash command (Manage Server)
- **Stoat**: `/link-emote <service> <external_id> <local_id>` message command (Manage Server)
- **IRC**: `LINK_EMOTE <service> <external_id> <local_id>`, DM (IRC-operator)

## `/mirror-channel <service|all> [local_id]`

Ensures a linked counterpart of the invoking channel exists on `service`
(or every other configured connector, if `all`) — creating one via that
connector's `ensure_channel` hook if it doesn't already have a matching
channel, then linking it. A service that can't create channels (Discord
has no channel-creation capability in this codebase) or hits a link conflict
is reported per-connector rather than aborting the rest when `all` is used.

- **Discord**: `/mirror-channel` slash command (Manage Server; `service`'s
  autocomplete includes the literal `all` choice)
- **Stoat**: `/mirror-channel <service|all> [local_id]` message
  command (Manage Server)
- **IRC**: `MIRROR_CHANNEL <local_id> <service|all>`, DM
  (IRC-operator; channel always required - no "current channel" to default
  to - and hoisted to the first arg since it's the one id IRC can't leave
  out)

## `/link-category <service> <external_id> [<local_id>]`

**Discord and Stoat only** (IRC has no Category concept). Links the invoking
channel's Category to `external_id`'s Category on connector `<service>` - or to
`<local_id>`'s Category on the connector the command is run on, if
given. Same already-linked/conflicting-group rules as `/link-channel`. Once
two Categories are linked, any **new channel** created inside either one is
automatically mirrored (created + linked, via the same logic as
`/mirror-channel`) into every other connector's own linked Category - no
manual `/mirror-channel` needed per new channel.

A Category that Discord's thread/forum-post auto-mirroring created on Stoat
(see the README's Discord threads section) can never be linked this way -
`/link-category` rejects it as both a `service`/`local_id` and as the
invoking channel's own Category, so thread mirroring's synthetic "Threads"
Categories always stay outside the bridge.

- **Discord**: `/link-category` slash command (Manage Server); the Category
  is always the invoking channel's own Category (the command must be run
  from inside a channel that's in one).
- **Stoat**: `/link-category <service> <external_id> [<local_id>]`
  message command (Manage Server); same "invoking channel's own Category"
  rule.
- **IRC**: not available - IRC has no Category concept.

## `/linked-categories`

Read-only listing of every Category linked to the invoking channel's own
Category, across every connector in its bridge group.

- **Discord**: `/linked-categories` slash command (defaults to the current
  channel's Category)
- **Stoat**: `/linked-categories` message command (defaults to the current
  channel's Category)
- **IRC**: not available - IRC has no Category concept.

## `/unlink-category [service|all]`

Removes members from the invoking channel's own Category's bridge group.
Given a specific `service` (a connector id), kicks just that one member
out - the rest of the group stays linked to each other. With no argument, or
`all` (the default), dissolves the whole group instead. Existing channels
already synced into the Category are left alone either way - only future
auto-sync stops.

- **Discord**: `/unlink-category [service]` slash command (Manage
  Server); `service`'s autocomplete includes the literal `all` choice.
- **Stoat**: `/unlink-category [service|all]` message command (Manage
  Server).
- **IRC**: not available - IRC has no Category concept.

## `/mirror-channels <service>`

**Stoat-only**, and distinct from `/mirror-channel` (singular) above: recreates
`<service>`'s (a configured Discord connector's) *entire current
category/channel layout* on the Stoat server this is run on
(`channel_structure.py`) — additive/idempotent, existing categories/channels
matched by name are left alone, nothing deleted or renamed. Every channel it
creates **or matches by name** is also linked back to its Discord
counterpart (same underlying logic as `/link-channel`), so this command
alone both creates and bridges a Stoat server's structure from Discord. A
channel already linked to a *different* group is skipped (reported in the
summary), not overwritten.

Discord forum channels have no Stoat equivalent, so each forum mirrors as
its own group named after the forum, with one channel per currently active
(non-archived) post.

- **Stoat**: `/mirror-channels <service>` message command (Manage Server) —
  not available on Discord or IRC (Discord doesn't need to mirror structure
  onto itself; IRC networks don't offer bot-driven channel creation the way
  this command needs).

## Roles: `/link role`, `/mirror role`, `/linked roles`, `/unlink role`

**Discord and Stoat only** (IRC has no role concept, same as Categories).
These use a space-separated subcommand syntax with the **local** id first,
and every id argument also accepts a bare **role name** (resolved
case-insensitively; first match wins, since Discord role names aren't
unique - pass an id when a name is ambiguous). Same already-linked /
conflicting-group rules as `/link-channel`.

On Discord these are `app_commands` subcommand groups (`/link role`,
`/unlink role`, `/linked roles`, `/mirror role` - typed exactly like that);
on Stoat they are the same tokens as a plain chat message. (The **user**
commands - `/link user` etc. - use this same subcommand shape; channel,
category and emote linking still use the flat `/link-channel` form, and a
later step migrates those too.)

### `/link role <local_id|name> <service> <external_id|name>`

Links `service`'s role to a local role. Manage Server (Discord) / Manage
Server (Stoat).

### `/mirror role <local_id|name> [<service>|all]`

Ensures a linked counterpart of the role exists on `service` (or every other
connector, if `all` - the default): reuses a same-named role there or creates
a bare one (name only - color/permissions are not copied), then links it. A
connector that can't create roles is reported per-connector. Manage Server.

### `/linked roles [<local_id|name>]`

Read-only. With a role, lists its linked counterparts; with no argument,
lists every linked-role group.

### `/unlink role <local_id|name> [<service>|all]`

Removes members from the role's bridge group - a specific `service` kicks
just that one, `all` (the default) dissolves the whole group. A kick that
would stand a lone survivor dissolves the group instead. The roles
themselves are never deleted. Manage Server.

- **Discord**: `/link role` / `/mirror role` / `/linked roles` /
  `/unlink role` slash subcommands (Manage Server on all but `/linked roles`).
- **Stoat**: the same `/link role …` etc. as message commands.
- **IRC**: not available - IRC has no role concept.

Once roles are linked, three things happen automatically (all best-effort and
silent):

- **auto-grant**: a linked user gaining/losing a linked role on one connector
  has the linked role granted/revoked for their linked identity (`/link user`)
  on the other. The Discord→other direction needs Discord's privileged
  **members** intent enabled for the bot.
- **rename**: renaming a linked role on one connector renames every linked
  copy to match.
- **delete**: deleting a linked role drops just that connector's link entry
  (the counterpart roles are left alone); a link left with a single member is
  dissolved.
- **permission mirroring**: changing a linked role's permission override on a
  bridge-linked channel or category mirrors the allow/deny onto the linked
  channel's copy for the linked role on the other connector - only the small
  set of permission bits that mean the same on Discord and Stoat; every other
  bit on the target is left untouched.

## `/unlink-channel [service|all] [local_id]`

Removes members from `local_id`'s (or the invoking channel, if
omitted) bridge group. Given a specific `service` (a connector id),
kicks just that one member out - the rest of the group, including the
channel itself, stays linked to each other. With no argument, or `all` (the
default), dissolves the whole group instead - every member is unlinked.
There's no separate "just leave, don't destroy the group for everyone else"
form beyond passing your own connector as `service`, which does exactly
that.

When a kick leaves a single member alone, that lone member isn't a bridge
anymore, so the group is dissolved outright rather than left as a group of
one. Any channel that ends up with no linked counterparts is announced to
its connector; **IRC** acts on that by posting a `This channel was unlinked
from ...` notice into the channel and then leaving it (PART) - it applies no
matter which connector ran the `/unlink-channel`. Discord/Stoat leave their
channels in place (they're real, human-created channels there).

- **Discord**: `/unlink-channel [service] [local_id]` slash
  command (Manage Server); `service`'s autocomplete includes the
  literal `all` choice, same as `/mirror-channel`. `local_id`
  defaults to the current channel.
- **Stoat**: `/unlink-channel [service|all] [local_id]` message
  command (Manage Server). `local_id` defaults to the current
  channel.
- **IRC**: `UNLINK_CHANNEL <local_id> [service|all]`, DM
  (IRC-operator; channel always required - no "current channel" to default
  to - and hoisted to the first arg since it's the one id IRC can't leave
  out; `service` remains optional and comes after)

## `/unlink user [service|all] [local_id|name]`

Removes identities from a user's cross-connector link group. Given a
specific `service` (a connector id), kicks just that one identity out -
the rest of the group stays linked to each other. With no `service`, or
`all` (the default), dissolves the whole group instead. `local_id` defaults to
whoever ran the command.

- **Discord**: `/unlink user [service] [local_id]` slash subcommand (Manage
  Server); `service`'s autocomplete includes the literal `all` choice.
  `local_id` is a real `discord.Member`, same picker as `/link user`'s local
  side and `/linked users`.
- **Stoat**: `/unlink user [service|all] [local_id|name]` message command
  (Manage Server) - `local_id` accepts a name or an id (no member-picker
  equivalent exists there, same caveat as `/link user`'s local side).
- **IRC**: `UNLINK USER [service|all] [local_id]`, DM
  (IRC-operator; both arguments optional, `local_id` defaults to the
  nick running the command)

## `HELP` (IRC) / `/bridge-help` (Stoat)

Prints a compact copy of this file's command list for that connector, since
neither has Discord's native slash-command discoverability. Read-only, no
permission gate.

- **Discord**: not needed - slash commands are self-documenting.
- **Stoat**: `/bridge-help` message command.
- **IRC**: `HELP`, sent as a DM to the bot.
