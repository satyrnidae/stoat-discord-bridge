# Commands

Every admin/status command the bridge exposes, and how to reach it on each
connector. Shared logic lives in `src/stoat_discord_bridge/admin_commands.py`
(`ChannelLinker` / `CategoryLinker` / `EmoteLinker` / `UserLinker` /
`RoleLinker`); each
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
id on the connector the command is run on. On Discord both are also
autocompleted: `<external_id>` offers the real channels / roles / users /
Categories / emoji on the connector picked in `<service>` (so pick
`<service>` first), and `<local_id>` offers this guild's own. Autocomplete
covers Discord and Stoat connectors; an IRC `<service>` returns no
suggestions (type the `#channel` / nick directly). Every id argument still
accepts a value typed by hand — an id, or a bare name.

## Conventions by connector

- **Discord**: slash commands. Anything that changes bridge state requires
  the Manage Server permission; read-only commands (`/status`,
  `/linked channels`, `/linked users`) don't.
- **Stoat**: message commands (type the command as a plain chat message,
  `/` prefix by default — configurable per connector via `command_prefix`,
  e.g. `!link channel …`). Parsed by `stoat.ext.commands` - `/link`, `/unlink`, `/linked`,
  `/mirror` are real command groups with `channel` / `role` / `user` /
  `category` / `emote` subcommands, the same shape as Discord's `app_commands`
  groups. Same Manage Server / read-only split as Discord. The invoking
  message and the bot's reply are never relayed to other connectors.
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
  IRC instead - and, for `LINK CHANNEL` / `MIRROR CHANNEL` / `UNLINK CHANNEL`
  / `LINKED CHANNELS`, is hoisted to the first position in IRC's syntax since
  it's the one argument IRC can't let slide.

## `/status`

Reports sync target health (`healthy` / `degraded` / `failing`) per
connector, tracked in `status.py`'s `HealthTracker` from each sender's
connection state and recent relay outcomes. No permission gate — read-only.

- **Discord**: `/status` slash command
- **Stoat**: `/status` message command
- **IRC**: `STATUS`, sent as a DM to the bot

A `GET /status` JSON endpoint on the health-check server (see the Docker
section of `README.md`) mirrors the same data.

## `/link channel [<local_id>] <service> <external_id>`

Uses the space-separated subcommand syntax with the **local** id first (like
the role commands). Every id argument also accepts a bare **channel name**
(resolved case-insensitively via each connector's own channel list; first
match wins - pass an id when a name is ambiguous).

Links `external_id` on connector `<service>` to `<local_id>` on the
connector the command is run on — or to the current channel if
`<local_id>` is omitted (Discord/Stoat only; IRC has no "current
channel" for a DM, so it's always required there). If either channel is
already linked, the existing bridge group is reused; if *both* are already
linked to two *different* groups, the command fails rather than merging them
(unlink one side first with `/unlink channel`).

- **Discord**: `/link channel` slash subcommand under the `/link` group
  (Manage Server). Discord lists required options first, so its option order
  is `service`, `external_id`, `local_id?`.
- **Stoat**: `/link channel [<local_id|name>] <service> <external_id|name>`
  message command (Manage Server)
- **IRC**: `LINK CHANNEL <local_id> <service> <external_id>`, DM (IRC-operator;
  local id required and first)

## `/linked channels [<local_id>]`

Read-only listing of every channel bridged to `<local_id>` (or the invoking
channel, if omitted), across every connector in its bridge group. `<local_id>`
also accepts a bare channel name.

- **Discord**: `/linked channels` slash subcommand (defaults to the current channel)
- **Stoat**: `/linked channels [<local_id|name>]` message command (defaults to the current channel)
- **IRC**: `LINKED CHANNELS <local_id>`, DM (channel always required)

## Users: `/link user`, `/linked users`, `/unlink user`

Links a user's identity across connectors, for two things: `services/
mentions.py`'s @mention rewriting, and making a message from the linked
remote user masquerade as the local identity when relayed (webhook
username+avatar on Discord, masquerade name+avatar on Stoat, the `<nick>`
prefix on IRC) instead of showing the remote user's own name/avatar. Same
already-linked/conflicting-group rules as `/link channel`.

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


## Emotes: `/link emote`, `/mirror emote`, `/linked emotes`, `/unlink emote`

**Discord and Stoat only** (IRC has no custom-emoji concept, same as roles and
Categories). Like the role commands, these use a space-separated subcommand
syntax, and **every id argument also accepts a bare emoji name** (resolved
case-insensitively; first match wins - pass an id when a name is ambiguous).
Same already-linked / conflicting-group rules as `/link channel`.

On Discord these are `app_commands` subcommand groups (`/link emote`,
`/mirror emote`, `/linked emotes`, `/unlink emote`); on Stoat they are the
same tokens as a plain chat message. They share the `/link` / `/mirror` /
`/linked` / `/unlink` groups with the channel, role, user and Category
commands.

### `/link emote <service> <external_id|name> <local_id|name>`

Links a custom emoji from connector `<service>` to a local custom emoji, so a
reaction using either can be recreated as the other (see the reaction/emoji
sync section of `README.md`/`CLAUDE.md`). Manage Server.

### `/mirror emote to <local_id|name> [<service>|all]` / `/mirror emote from <service> <external_id|name>`

`to` ensures a linked counterpart of the local emoji exists on `<service>`
(or every other connector, if `all` - the default): reuses the existing link
if the pair is already linked; failing that, links to a same-named emoji that
already exists on the destination (name match only - images aren't compared)
rather than creating a duplicate; only if neither is found does it read the
source emoji's image, recreate it on the destination, and link the two.
`from` is the same operation run the other way - `<service>`'s emoji is read
and recreated-or-matched **here**, then linked (reusing an existing mapping
group). Unlike `/mirror role`, an emoji
can't be created name-only - a connector that can't read the source emoji or
can't create it (slots full, name rejected, image too large) is reported
per-connector. Manage Server.

### `/linked emotes [<local_id|name>]`

Read-only. With an emote, lists its linked counterparts; with no argument,
lists every linked-emote group.

### `/unlink emote <local_id|name> [<service>|all]`

Removes members from the emoji's mapping group - a specific `<service>` kicks
just that one, `all` (the default) dissolves the whole group. A kick that
would stand a lone survivor dissolves the group instead. The emoji
themselves are never deleted.

- **Discord**: the `/link emote` / `/mirror emote to` / `/mirror emote from` /
  `/linked emotes` / `/unlink emote` slash subcommands (Manage Server on all
  but `/linked emotes`).
- **Stoat**: the same tokens as message commands.
- **IRC**: not available - IRC has no custom emoji.

## `/mirror channel to` / `/mirror channel from`

`/mirror <noun>` is a two-way group on every connector kind: `to` pushes a
**local** entity onto another connector; `from` pulls a **remote** entity in
and creates the local copy. On Discord `channel` / `role` / `category` /
`emote` are each an `app_commands` subcommand group with `to` and `from`
under them; on Stoat they're the same tokens as a chat message; on IRC only
`MIRROR CHANNEL` exists, taking `TO` / `FROM` as its first token.

### `/mirror channel to [<service>|all] [<local_id>]`

Ensures a linked counterpart of `<local_id>` (or the invoking channel, if
omitted) exists on `service` — or every other configured connector, if
`all`, which is also the default when `service` is omitted — creating one via
that connector's `ensure_channel` hook if it doesn't already have a matching
channel, then linking it. A service that can't create channels (Discord
has no channel-creation capability in this codebase) or hits a link conflict
is reported per-connector rather than aborting the rest when `all` is used.
`<local_id>` also accepts a bare channel name. Both arguments are optional and
`<service>` leads (matching `from`'s shape); a lone argument is read as
`<service>`, so pass `all` explicitly to name just a channel
(`/mirror channel to all my-channel`).

### `/mirror channel from <service> <external_id>`

The inbound direction: `<service>`'s `<external_id>` channel already exists,
so a linked counterpart is created **on the connector the command is run on**
(via its own `ensure_channel` hook) and the two are linked — reusing an
existing bridge group if `<external_id>` is already in one. "Respecting other
linked entities": if the source channel sits in a Category that's already
linked (via `/link category`) to a Category here, the new local channel is
placed into *that* linked Category rather than a fresh same-named one.
`<external_id>` also accepts a bare channel name. There's no `all` form -
`from` always names one source.

- **Discord**: `/mirror channel to` / `/mirror channel from` subcommands
  under the `/mirror channel` group (Manage Server; `to`'s `service`
  autocomplete includes the literal `all` choice, `from`'s doesn't)
- **Stoat**: `/mirror channel to [<service>|all] [<local_id|name>]` /
  `/mirror channel from <service> <external_id|name>` message commands
  (Manage Server)
- **IRC**: `MIRROR CHANNEL TO [<service>|all] <local_id>` /
  `MIRROR CHANNEL FROM <service> <external_id>`, DM (IRC-operator; `TO`'s
  local id is always required - no "current channel" to default to - so a
  lone `TO` argument is the id and `service` defaults to `all`)

## Categories: `/link category`, `/mirror category`, `/linked categories`, `/unlink category`

**Discord and Stoat only** (IRC has no Category concept). Like the role
commands below, these use a space-separated subcommand syntax, and **every id
argument also accepts a bare Category name** (resolved case-insensitively;
first match wins - pass an id when a name is ambiguous). Same already-linked /
conflicting-group rules as `/link channel`.

On Discord these are `app_commands` subcommand groups (`/link category`,
`/mirror category`, `/linked categories`, `/unlink category` - typed exactly
like that); on Stoat they are the same tokens as a plain chat message. The
Category defaults to the invoking channel's own where an explicit
`<local_id|name>` is omitted.

A Category that Discord's thread/forum-post auto-mirroring created on Stoat
(see the README's Discord threads section) can never be linked this way -
`/link category` rejects it, so thread mirroring's synthetic Categories always
stay outside the bridge.

### `/link category <service> <external_id|name> [<local_id|name>]`

Links the invoking channel's Category (or `<local_id|name>`'s Category on this
connector, if given) to `<external_id|name>`'s Category on `<service>`. Once
two Categories are linked, any **new channel** created inside either one is
automatically mirrored (created + linked, same logic as `/mirror channel`)
into every other connector's own linked Category. Manage Server.

### `/mirror category to [<service>|all] [<local_id|name>]` / `/mirror category from <service> <external_id|name>`

`to` ensures a linked counterpart of the local Category exists on `<service>`
(or every other connector, if `all` - the default; both arguments are
optional and `<service>` leads, so a lone argument is read as `<service>`):
reuses the existing
linked Category if the pair is already linked, otherwise creates a same-named
one (name only) and links it. Then relocates the source Category's channels
onto that destination Category - a child already linked to a `<service>`
channel is *moved* into it, an unlinked child is mirrored (created + linked)
there. `from` is the same operation run the other way - a local counterpart
of `<service>`'s Category is created **here**, linked, and `<service>`'s
channels relocated/mirrored into it. A connector that can't create Categories
is reported per-connector. Manage Server.

### `/linked categories [<local_id|name>]`

Read-only listing of every Category linked to the given (or invoking) Category,
across every connector in its bridge group.

### `/unlink category [<local_id|name>] [<service>|all]`

Removes members from the Category's bridge group. A specific `<service>` kicks
just that one member out; `all` (the default) dissolves the whole group.
Existing channels already synced into the Category are left alone either way -
only future auto-sync stops. Manage Server.

- **Discord**: the `/link category` / `/mirror category to` /
  `/mirror category from` / `/linked categories` / `/unlink category` slash
  subcommands (Manage Server on all but `/linked categories`).
- **Stoat**: the same tokens as message commands.
- **IRC**: not available - IRC has no Category concept.

## Roles: `/link role`, `/mirror role`, `/linked roles`, `/unlink role`

**Discord and Stoat only** (IRC has no role concept, same as Categories).
These use a space-separated subcommand syntax with the **local** id first,
and every id argument also accepts a bare **role name** (resolved
case-insensitively; first match wins, since Discord role names aren't
unique - pass an id when a name is ambiguous). Same already-linked /
conflicting-group rules as `/link channel`.

On Discord these are `app_commands` subcommand groups (`/link role`,
`/unlink role`, `/linked roles`, `/mirror role` - typed exactly like that);
on Stoat they are the same tokens as a plain chat message. The channel
commands (`/link channel` etc.) and the user commands (`/link user` etc.)
share the same `/link` / `/unlink` / `/mirror` / `/linked` groups.

### `/link role <local_id|name> <service> <external_id|name>`

Links `service`'s role to a local role. Manage Server (Discord) / Manage
Server (Stoat).

### `/mirror role to <local_id|name> [<service>|all]` / `/mirror role from <service> <external_id|name>`

`to` ensures a linked counterpart of the local role exists on `service` (or
every other connector, if `all` - the default): reuses a same-named role
there or creates a bare one (name only - color/permissions are not copied),
then links it. `from` is the same operation run the other way - a local
counterpart of `<service>`'s role is created-or-matched **here** and linked
(reusing an existing bridge group). A connector that can't create roles is
reported per-connector. Manage Server.

### `/linked roles [<local_id|name>]`

Read-only. With a role, lists its linked counterparts; with no argument,
lists every linked-role group.

### `/unlink role <local_id|name> [<service>|all]`

Removes members from the role's bridge group - a specific `service` kicks
just that one, `all` (the default) dissolves the whole group. A kick that
would stand a lone survivor dissolves the group instead. The roles
themselves are never deleted. Manage Server.

- **Discord**: `/link role` / `/mirror role to` / `/mirror role from` /
  `/linked roles` / `/unlink role` slash subcommands (Manage Server on all
  but `/linked roles`).
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

## `/unlink channel [<local_id>] [<service>|all]`

Removes members from `local_id`'s (or the invoking channel, if
omitted) bridge group. `<local_id>` also accepts a bare channel name. Given a specific `service` (a connector id),
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
matter which connector ran the `/unlink channel`. Discord/Stoat leave their
channels in place (they're real, human-created channels there).

- **Discord**: `/unlink channel` slash subcommand under the `/unlink` group
  (Manage Server); `service`'s autocomplete includes the literal `all`
  choice, same as `/mirror channel to`. `local_id` defaults to the current
  channel.
- **Stoat**: `/unlink channel [<local_id|name>] [<service>|all]` message
  command (Manage Server). `local_id` defaults to the current
  channel.
- **IRC**: `UNLINK CHANNEL <local_id> [<service>|all]`, DM
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
