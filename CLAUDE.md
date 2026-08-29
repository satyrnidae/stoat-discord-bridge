# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A multi-way chat bridge across any number of Discord, Stoat, and IRC servers,
configured entirely in `config.yaml` (no code changes needed to add another
server). Incoming messages are relayed into Discord "as" the originating
Stoat/IRC user via per-channel webhooks (username + avatar override), rather
than posting under the bridge bot's own identity.

**Status: scaffolding only.** Discord/Stoat sender connections and the
Discord/Stoat receivers (webhook/masquerade posting) work. IRC's receiver and
asyncio integration are implemented but unverified against a live server.
Reaction and custom-emoji sync (`services/discord_service.py`,
`services/stoat_service.py`, `bridge.py`) is implemented against a best guess
at `stoat.py`'s event names and object shape, flagged with `TODO`s where
unconfirmed — same caveat applies to the rest of the Stoat integration and the
WHOIS-based IRC-operator check backing IRC's admin DM commands' permission gate.

## Commands

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -e ".[test]"
copy .env.example .env
copy config.yaml.example config.yaml
# fill in .env with real bot tokens, fill in config.yaml with your actual
# deployment's ids/hosts (it's gitignored - see config.yaml.example), then:
python -m stoat_discord_bridge
```

Tests: `pytest` (config lives in `pyproject.toml`'s `[tool.pytest.ini_options]`).
Covers the pure-logic layer - `config.py`'s env/YAML resolution, the
storage repositories (against an in-memory fake Mongo, `tests/conftest.py`),
`admin_commands.py`'s linkers, `services/mentions.py`, and a few
network-free pieces of the service modules (IRC's WHOIS-based oper check,
Stoat's websocket-gateway discovery). Does not cover the actual
Discord/Stoat/IRC network integration - no live-server or full-client-mock
tests exist yet, so those still need manual verification. No linter or CI
config in this repo yet.

Docker: `docker compose up --build` runs the bridge plus a MongoDB
instance (data persisted in a named volume) - see the README's Docker
section. `config.yaml`/`certs/` are bind-mounted, `.env` loaded via
`env_file`; `docker-compose.yml` forces `MONGO__URI` to the containerized
Mongo regardless of what `.env` says, so the bridge always gets a working
Mongo under Docker without needing one set up separately. The image bundles
the 1Password `op` CLI (opt out with `--build-arg INSTALL_OP=0`) so
`op://...` values in `config.yaml` resolve; point
`OP_SERVICE_ACCOUNT_TOKEN_FILE` at a mounted secret to authenticate it
(`config.py` loads that file into `OP_SERVICE_ACCOUNT_TOKEN`).

## Architecture

Service-based: each configured connector (a Discord guild, Stoat server, or
IRC network entry in `config.yaml`) gets a **sender** service (listens to it,
turns native events into a `StandardMessage`) and a **receiver** service
(takes a `StandardMessage` and posts it into that connector). `SenderService`
and `ReceiverService` (`services/base.py`) are the abstract base classes both
sides implement, one pair per connector kind
(`discord_service.py` / `stoat_service.py` / `irc_service.py`).

`BridgeCoordinator` (`bridge.py`) wires every sender's output to every other
connector's receiver, looking up which channels are bridged together via
MongoDB (`storage/channel_mappings.py`). Reactions and custom emoji are
*optional* receiver capabilities gated by `supports_reactions` /
`supports_emoji` flags on `ReceiverService` — `BridgeCoordinator` checks these
before calling in, so a connector kind that doesn't advertise support (IRC
has neither) never hits the default "raises `NotImplementedError`" path.

Messages move between services as a `StandardMessage` (`models.py`) — a
platform-neutral shape carrying sender profile image, channel name, sender
display name/username/nickname, Markdown content, attachment data/URLs, and a
message ID for sync tracking. Platform-specific particularities (stripping
Markdown, inlining attachment URLs, splitting long messages for IRC, etc.)
are handled inside each receiver's `receive()`, not in the shared message
format. `receive()` returns every native message ID it posted (a message may
be split across multiple platform posts) and raises `PartialRelayError`
rather than silently losing IDs if a later post in a split fails after
earlier ones succeeded.

`config.py` loads `config.yaml` and layers env vars over it per-field: an
`{SECTION}__{index}__{FIELD}` env var (Azure App Configuration/ASP.NET
Core-style hierarchical binding — `index` is the connector's 0-based
position within its kind's `config.yaml` list, e.g. `STOAT__1__TOKEN` for
the 2nd `stoat:` entry) beats a literal value written directly in
`config.yaml`. A
`{SECTION}__{index}__{FIELD}_FILE` env var naming a file is a third source,
and any resolved value that looks like a 1Password secret reference
(`op://<vault>/<item>/<field>`) is dereferenced via the `op` CLI at startup
(opt-in — `op` is only invoked if such a value is present). This means any
field — not just tokens — can live in
`config.yaml` or in a positional env var, connector-by-connector. Adding
another server of any kind is just another `config.yaml` list entry (or
purely env vars, if you'd rather). Connector IDs must be unique across all
three kinds combined. `config.yaml` is itself gitignored — see
`config.yaml.example` for the template and full field list.

### Reaction & custom emoji sync

Discord and Stoat reactions are mirrored onto every other connector's copy of
the same message (via `MessageSyncRepository`, which tracks cross-connector
message IDs); custom emoji created on one connector are mirrored onto the
others so a reaction using them can be recreated at all. Both directions are
best-effort and silently skip rather than error — a reaction on a message the
bridge never relayed is dropped; a custom emoji a target connector can't
create (slots full, name rejected, image too large, etc.) is skipped on that
connector only; a reaction using a custom emoji never successfully mirrored
onto a given target is ignored for that target. **Deleting** a custom emoji
is never mirrored onto other connectors (a copy still in use elsewhere keeps
working) — it only updates `EmojiMappingRepository`'s bookkeeping via
`forget()` for the connector it was deleted on; the cross-connector mapping
itself drops only once every connector's copy has been deleted.

### Admin & status commands

Every admin/status command (`/status`, the channel commands `/link channel` /
`/unlink channel` / `/mirror channel` / `/linked channels`, the role commands
`/link role` / `/mirror role` / `/linked roles` / `/unlink role`
(Discord/Stoat only), `/link-user`, `/unlink-user`, `/linked-users`,
`/link-emote`, `/mirror-channels`, `/link-category`, `/unlink-category`,
`/linked-categories`) and how to reach it on each connector is documented in
`COMMANDS.md`, not duplicated here. On Discord the channel and role commands
are real `app_commands` subcommand groups (`/link`, `/unlink`, `/mirror`,
`/linked`); on Stoat/IRC they're space-separated (`LINK CHANNEL …` on IRC).
Category/emote/user commands are still flat. Every id argument to a channel
or role command also accepts a bare name (`ConnectorInfo.
resolve_channel_id_by_name` / `resolve_role_id_by_name`). Shared logic
lives in `admin_commands.py` (`ChannelLinker` / `CategoryLinker` /
`EmoteLinker` / `UserLinker` / `RoleLinker` / `StructureMirrorer`), called
identically from each connector's own `services/*.py` module. Nothing is
bridged (or mention-linked) automatically — every pair is linked explicitly
via those commands.

`RoleLinker` (`storage/role_mappings.py`) is the role-level counterpart of
`ChannelLinker`. Every id argument also accepts a bare role name via each
connector's `resolve_role_id_by_name` hook; `/mirror role` creates-or-matches
a same-named role via the `ensure_role` hook (name only — color/permissions
aren't copied). Linked-role `<@&id>` / `<%id>` mentions are rewritten into the
target's linked role (`@Name` on IRC) alongside the user/channel mention
rewrites.

`RoleSyncCoordinator` (`bridge.py`) keeps linked roles coherent:

- **auto-grant** (`handle`): a cross-connector-linked user gaining/losing a
  cross-connector-linked role on one connector (Discord `on_member_update` /
  Stoat `on_server_member_update`) has the linked role granted/revoked for
  their linked identity on every other connector via the `grant_role` /
  `revoke_role` hooks.
- **rename** (`handle_role_renamed`, Discord `on_guild_role_update` / Stoat
  `on_raw_server_role_update`): a linked role renamed on one connector is
  renamed to match on every linked copy (`rename_role` hook) and the stored
  `role_name` is refreshed.
- **delete** (`handle_role_deleted`, `on_guild_role_delete` /
  `on_server_role_delete`): drops just that connector's mapping entry — the
  counterpart roles stay (they may still be in use); a group left with ≤ 1
  member is dissolved. Roles are never auto-created on creation.
- **permission mirroring** (`handle_channel_role_permission`, Discord
  `on_guild_channel_update` / Stoat `on_channel_update`): a linked role's
  permission override on a bridge-linked channel/category changing on one
  connector is mirrored onto the linked channel's copy for the linked role
  on the other, via the `get_channel_role_permission` /
  `set_channel_role_permission` hooks. Only the bits in
  `services/role_sync.NEUTRAL_PERMISSIONS` (the ones that mean the same on
  both platforms) are touched; every other bit on the target's override is
  preserved (`RolePermissionOverride.splice_onto`).

All best-effort and silent (unlinked user/role/channel, missing hook, or a
raising hook are skipped). Loop-safe two ways: each hook is idempotent (no-op
if already in the desired state), and the coordinator keeps a ~10s record of
writes it issued so the echo event is dropped. **Discord needs the privileged
members intent** (enabled on `_DiscordClient` and in the developer portal) or
the Discord→other direction of auto-grant never fires.

The `services/role_sync.py` permission-name translation is a deliberately
conservative subset. stoat.py's member/role/channel gateway events are
assumed from `stoat.events` and unverified against a live server (`TODO`s in
`stoat_service.py`).

`ChannelLinker.unlink_channel` dissolves a bridge group down to nothing
rather than leaving a lone member (a group of one isn't a bridge), and fires
`ConnectorInfo.on_channel_unlinked(channel_id, unlinked_from)` for every
channel left with no linked counterparts — regardless of which connector ran
the command. Only IRC wires it (`IrcSenderService.part_channel`): it posts a
`This channel was unlinked from …` notice and PARTs. Discord/Stoat leave
their channels alone.

On IRC, a channel the bridge's own JOIN created gets
`default_channel_modes` applied; the `P` (InspIRCd permanent-channel) mode,
if configured, is split off and applied separately once the OPER handshake
is confirmed (`on_youreoper` — it's oper-only, and channels created before
then are parked in `_pending_permanent_modes`), and is withheld from
ephemeral Discord-thread channels (`ensure_channel`'s `is_thread_category`).

Category linking (`/link-category`) is Discord/Stoat-only (IRC has no
Category concept) and, unlike channel linking, has an automatic-sync side
effect: once two Categories are linked, a new channel created inside either
one is auto-mirrored (created + linked) into every other connector's own
linked Category, via the same `ChannelLinker.mirror_channel` logic
`/mirror channel` uses. `CategoryLinker.link_category` refuses to link a
Category that `ThreadCategoryRepository` has marked as a thread category
(see below) — those stay outside the bridge.

### Discord threads

Discord threads have no IRC/Stoat equivalent. `_handle_thread_create`
(`services/discord_service.py`) treats a Discord thread/forum-post as a new
Stoat/IRC channel rather than trying to map it onto their flat channel model:
it auto-mirrors (creates + links) the channel on every other connector via
`ChannelLinker.mirror_channel_all`, placed under a Category named after the
thread's **parent channel** (so every thread under one parent groups together
on the destination) — using each destination's *own* linked name for that
parent channel (`mirror_channel`'s `category_from_channel_id`), not the Discord
name, and falling back to the Discord name only where the parent isn't linked
there. It then relays the thread's own starter message into it as
the originating user. On Stoat, if `group_parent_channel_with_threads` is set
(default on, per-connector), the parent channel itself is also moved into that
Category at the top — re-checked on every relayed message
(`StoatSenderService.group_parent_channel_with_threads`, called from the
receiver) so enabling it mid-deployment takes effect without a restart. Discord's own "<user> started a thread" system message in
the parent channel is suppressed (`_handle_message`); `_handle_thread_create`
instead posts its own bot notice — `"<user> started a thread: <#thread>"` — but
only *after* the mirror+link finishes, so the `<#thread>` mention resolves. Each
receiver rewrites that mention (`services/mentions.py`'s
`rewrite_channel_mentions`, run alongside the user-mention rewrite) into its own
linked copy of the mirrored channel — `<#id>` on Discord/Stoat, `#channel` on
IRC — falling back to `#<thread name>` if it still can't resolve. A thread with
no real starter message (standalone thread / forum-post system row) skips the
starter relay but keeps that row's author for the notice. Only
fires when the thread's parent channel is itself already bridged; one-way
(Discord → Stoat/IRC). The destination Category is bound to the destination's
own parent channel id via `CategoryLinker.bind_thread_category` (backed by
`storage/category_mappings.py`'s `ThreadCategoryRepository`, keyed by
`(connector, parent_channel_id)`), which `/link-category` checks to refuse ever
linking it into the bridge. That binding is what later threads resolve the
Category by — **by id, not by title** — so renaming the Category on Stoat no
longer spawns a fresh one, and `group_parent_channel_with_threads` finds the
parent channel by its bound id rather than a name match. A bound Category that
has since been deleted self-heals: the next thread forgets the binding, creates
a fresh Category by the linked parent name, and rebinds (orphaned thread
channels are left where they are). Pre-binding rows (no `parent_channel_id`)
still register as thread categories and are rewritten to the bound shape on the
next thread for that parent.

## Layout

```
config.yaml                    # every configured connector (Discord/Stoat/IRC) - gitignored, see config.yaml.example
Dockerfile / docker-compose.yml # bridge + auto-provisioned Mongo - see README's Docker section
tests/                          # pytest suite - see README's Tests section
src/stoat_discord_bridge/
  config.py                    # loads config.yaml, layering env vars over it per-field (see its docstring)
  models.py                    # StandardMessage - the platform-neutral message format
  channel_structure.py         # GuildStructure snapshot used by the /mirror-channels command
  admin_commands.py            # ChannelLinker / CategoryLinker / EmoteLinker / UserLinker / RoleLinker / StructureMirrorer - shared linking & /mirror-channels logic
  bridge.py                    # BridgeCoordinator: routes StandardMessages sender -> receiver via channel mappings
  status.py                    # HealthTracker: per-connector sync target health, read by the /status commands
  services/
    base.py                    # SenderService / ReceiverService base classes
    formatting.py               # content formatting utilities shared across receivers
    discord_service.py          # Discord sender (client) + receiver (per-channel webhook), one instance per config.yaml entry
    stoat_service.py            # Stoat sender/receiver, one instance per config.yaml entry
    irc_service.py              # IRC sender/receiver, one instance per config.yaml entry
  storage/
    mongo.py                   # MongoDB connection (motor)
    channel_mappings.py        # which channels, across connectors, are bridged together
    category_mappings.py       # which categories, across connectors, are bridged together; ThreadCategoryRepository binds a thread's parent channel to its (unlinkable) thread-only category id
    role_mappings.py           # which roles, across connectors, are linked (Discord/Stoat only); backs /link role, role-mention rewriting, and auto-grant
    message_sync.py            # cross-connector message ID references, for edit/delete sync
    emoji_mappings.py          # cross-connector custom emoji ID references, for reaction sync
  services/role_sync.py        # network-free helpers for the role permission-mirror flow (neutral<->native permission translation)
```
