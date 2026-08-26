# Commands

Every admin/status command the bridge exposes, and how to reach it on each
connector. Shared logic lives in `src/stoat_discord_bridge/admin_commands.py`
(`ChannelLinker` / `EmoteLinker` / `UserLinker` / `StructureMirrorer`); each
connector's own `services/*.py` module just wires its native command syntax
to that shared logic, so behavior is identical everywhere except where noted.

A `<source>`/`destination` argument below is a connector `id` from
`config.yaml` (see its `id` field) — not a platform name, since there can be
any number of connectors of each kind. On Discord, every such argument has
autocomplete listing the bridge's currently configured connectors.

## Conventions by connector

- **Discord**: slash commands. Anything that changes bridge state requires
  the Manage Server permission; read-only commands (`/status`,
  `/linked-channels`, `/linked-users`) don't.
- **Stoat**: message commands (type the command as a plain chat message).
  Same Manage Server / read-only split as Discord.
- **IRC**: sent as a **DM to the bot**, bare and **uppercase**, no leading
  `/` or `!` (unlike Discord/Stoat's slash commands — many IRC clients treat
  a leading `/` as a local client command and never send it as text).
  Anything that changes bridge state requires **IRC-operator status**,
  checked live via `WHOIS` (not channel-operator status — a DM has no
  per-channel permission to check against in the first place); read-only
  commands need no permission. A DM also has no "current channel" the way a
  Discord/Stoat command run *in* a channel does, so any argument that would
  otherwise default to "the channel this was run in" is always required on
  IRC instead.

## `/status`

Reports sync target health (`healthy` / `degraded` / `failing`) per
connector, tracked in `status.py`'s `HealthTracker` from each sender's
connection state and recent relay outcomes. No permission gate — read-only.

- **Discord**: `/status` slash command
- **Stoat**: `/status` message command
- **IRC**: `STATUS`, sent as a DM to the bot

A `GET /status` JSON endpoint on the health-check server (see the Docker
section of `README.md`) mirrors the same data.

## `/link-channel <source> <source_id> [<destination_id>]`

Links `source_id` on connector `<source>` to `<destination_id>` on the
connector the command is run on — or to the current channel if
`<destination_id>` is omitted (Discord/Stoat only; IRC has no "current
channel" for a DM, so it's always required there). If either channel is
already linked, the existing bridge group is reused; if *both* are already
linked to two *different* groups, the command fails rather than merging them
(unlink one side first — there's no unlink command yet, so that currently
means linking one side elsewhere to move it into a different group).

- **Discord**: `/link-channel` slash command (Manage Server)
- **Stoat**: `/link-channel <source> <source_id> [<destination_id>]` message
  command (Manage Server)
- **IRC**: `LINK_CHANNEL <source> <source_id> <local_id>`, DM (IRC-operator)

## `/linked-channels`

Read-only listing of every channel bridged to the invoking channel, across
every connector in its bridge group.

- **Discord**: `/linked-channels` slash command (defaults to the current channel)
- **Stoat**: `/linked-channels` message command (defaults to the current channel)
- **IRC**: `LINKED_CHANNELS <local_channel_id>`, DM (channel always required)

## `/link-user <source> <user_id> <local_user>`

Links `source`'s `user_id` to a local identity, purely for `services/
mentions.py`'s @mention rewriting — never affects message relaying itself.
Same already-linked/conflicting-group rules as `/link-channel`.

- **Discord**: `/link-user` slash command (Manage Server). The local side
  (`local_user`) is picked from Discord's own member search — a real
  `discord.Member` option, not typed text. Deliberately: a free-text id
  field here previously let someone type a bare `@name` instead of the real
  snowflake, which silently linked a nonexistent id and broke mention
  rewriting in both directions with no error.
- **Stoat**: `/link-user <source> <user_id> <local_user_id>` message command
  (Manage Server) — the local side is still plain text (no member-picker
  equivalent exists there), so the same mistake above is still possible when
  linking *from* Stoat.
- **IRC**: `LINK_USER <source> <user_id> <local_user_id>`, DM (IRC-operator)

Relinking a connector's id within an existing group (e.g. correcting a
mistake like the one above) replaces the old entry rather than leaving both
on file for mention rewriting to pick between nondeterministically.

## `/linked-users [user]`

Read-only listing of cross-connector user links, for debugging. With no
target, lists every link group; given one, shows just that identity's group.
Each entry's display name is resolved **live** from its own connector
(`ConnectorInfo.resolve_user_name`) rather than read from storage, since the
stored name is never more than the id it was linked with.

- **Discord**: `/linked-users [user]` slash command (`user` a real `discord.Member`, optional)
- **Stoat**: `/linked-users [user_id]` message command
- **IRC**: `LINKED_USERS [local_user_id]`, DM

## `/link-emote <source> <source_id> <local_id>`

Links a custom emoji from connector `<source>` to a local custom emoji, so a
reaction using either can be recreated as the other (see the reaction/emoji
sync section of `README.md`/`CLAUDE.md`). Same already-linked/
conflicting-group rules as `/link-channel`.

- **Discord**: `/link-emote` slash command (Manage Server)
- **Stoat**: `/link-emote <source> <source_id> <local_id>` message command (Manage Server)
- **IRC**: `LINK_EMOTE <source> <source_id> <local_id>`, DM (IRC-operator)

## `/mirror-channel <destination|all> [local_channel_id]`

Ensures a linked counterpart of the invoking channel exists on `destination`
(or every other configured connector, if `all`) — creating one via that
connector's `ensure_channel` hook if it doesn't already have a matching
channel, then linking it. A destination that can't create channels (Discord
has no channel-creation capability in this codebase) or hits a link conflict
is reported per-connector rather than aborting the rest when `all` is used.

- **Discord**: `/mirror-channel` slash command (Manage Server; `destination`'s
  autocomplete includes the literal `all` choice)
- **Stoat**: `/mirror-channel <destination|all> [local_channel_id]` message
  command (Manage Server)
- **IRC**: `MIRROR_CHANNEL <destination|all> <local_channel_id>`, DM
  (IRC-operator; channel always required)

## `/mirror-channels <source>`

**Stoat-only**, and distinct from `/mirror-channel` (singular) above: recreates
`<source>`'s (a configured Discord connector's) *entire current
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

- **Stoat**: `/mirror-channels <source>` message command (Manage Server) —
  not available on Discord or IRC (Discord doesn't need to mirror structure
  onto itself; IRC networks don't offer bot-driven channel creation the way
  this command needs).
