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
`services/stoat_service.py`, `bridge.py`) is wired to `stoat.py`'s real
event/method names (`on_message_react`/`on_message_unreact`,
`on_server_emoji_create`/`on_server_emoji_delete`, `Message.react`/`unreact`,
`Message.reactions`), verified against the installed package but not yet
against a live server — same caveat applies to the rest of the Stoat
integration and the WHOIS-based IRC-operator check backing IRC's admin DM
commands' permission gate.

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
tests exist yet, so those still need manual verification. No linter config
in this repo yet; CI (`.github/workflows/ci.yml`) runs `pytest` on Python
3.11-3.13 plus an sdist/wheel build on every push to `main` and every PR.

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

Attachments arrive on a `StandardMessage` as URLs (a Discord/Stoat CDN link).
The Discord and Stoat receivers **re-upload** each one as a native file on the
relayed message (`services/formatting.download_attachments` fetches the bytes;
`webhook.send(files=…)` / `channel.send(attachments=…)`) rather than pasting
the link into the text — those signed CDN URLs expire, and a native file
renders inline — attaching them to the last post of a split message (issue
#39). Anything over `formatting._MAX_REUPLOAD_BYTES` (8 MiB) or that can't be
fetched falls back to an inlined URL via `formatting.inline_attachment_urls`
so it's never lost. IRC has no native attachments, so
`IrcReceiverService.receive` still inlines every attachment URL as its own
line.

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
others so a reaction using them can be recreated at all. An inline custom
emoji *in a relayed message's text* is likewise rewritten into the target's
linked copy (`services/mentions.py`'s `rewrite_emoji`, run alongside the
user/channel/role-mention rewrites, keyed off `EmojiMappingRepository`):
`<:name:id>` on Discord, `:id:` (bare 26-char ULID) on Stoat, and — since IRC
has no custom emoji — stripped there to a plain `:name:` shortcode (or removed
outright if the name can't be recovered) rather than left as a raw token. An
emoji with no link to a Discord/Stoat target is left exactly as it appeared.
Both directions are
best-effort and silently skip rather than error — a reaction on a message the
bridge never relayed is dropped; a custom emoji a target connector can't
create (slots full, name rejected, image too large, etc.) is skipped on that
connector only; a reaction using a custom emoji never successfully mirrored
onto a given target is ignored for that target; a Stoat *builtin* emoji
(the non-Unicode, non-custom `distorted_face`/`trollface` pack — classified
by `_parse_stoat_emoji` returning `None`) is dropped toward every other
connector. The bridge mirrors its reaction once (a second origin user
reacting with the same emoji is a no-op via `StandardReaction.origin_reactor_count`
in `BridgeCoordinator.handle_reaction`) and holds it until the last origin
user removes theirs; the `add_reaction`/`remove_reaction` receiver hooks are
independently idempotent as a backstop. **Deleting** a custom emoji
is never mirrored onto other connectors (a copy still in use elsewhere keeps
working) — it only updates `EmojiMappingRepository`'s bookkeeping via
`forget()` for the connector it was deleted on; the cross-connector mapping
itself drops only once every connector's copy has been deleted.

### Message pin sync

Pinning/unpinning a message in a bridged channel is mirrored onto every other
connector's copy of that message (`BridgeCoordinator.handle_pin` →
`ReceiverService.set_pinned`, gated by `supports_pins` and keyed off the same
`MessageSyncRepository` group reaction sync uses). Discord ⇄ Stoat only —
**IRC has no message-pin concept** (`supports_pins` stays `False`), so a pin
never routes to it. Best-effort and silent (an untracked message, a missing
`set_pinned` hook, or a raising one are all skipped); loop-safe the same two
ways as role sync — `set_pinned` is idempotent (no-op if already in that
state) and the coordinator keeps a ~10s record of writes it issued so the echo
event is dropped.

Each platform's pin action produces a *system message* that used to be relayed
as a blank message: Discord's `MessageType.pins_add` (suppressed in
`_handle_message`; the pin itself is picked up from `on_raw_message_edit`,
which also covers unpins — Discord has no `pins_remove` system message) and
Stoat's `message_pinned` / `message_unpinned` system events (detected via
`message.system_event` in `_handle_message` and turned into a `StandardPin`).
As a catch-all, `IrcReceiverService.receive` drops any synced message with no
textual content — which is how IRC ignores pin notifications from both sides.

### Message edit sync

Editing a message's *content* in a bridged channel is mirrored onto every
other connector's copy of that message (`BridgeCoordinator.handle_edit` →
`ReceiverService.edit_message`, gated by `supports_edits` and keyed off the
same `MessageSyncRepository` group reaction/pin sync use). Discord ⇄ Stoat
only — **IRC has no edit-in-place** (`supports_edits` stays `False`), so an
edit never routes to it (issue #62). Each sender emits a `StandardEdit`:
Discord from `on_raw_message_edit` when the payload carries a fresh `content`
*and* an `edited_timestamp` (the latter distinguishes a real user edit from
an auto-embed unfurl — and from a pin toggle, which carries `pinned`
instead); Stoat from `on_message_update` (`stoat.events.MessageUpdateEvent`,
preferring `event.after` over the partial `event.message`). The original
relay may have been split across several native posts in one channel —
`edit_message` gets the whole ordered list and re-renders the new text
through the same `_rewrite_content` helper `receive()` uses (user/channel/
role/emoji mention rewrites; attachments are *not* re-synced), matching one
chunk per post; a shortened edit blanks the leftover posts (zero-width
space), a grown one drops the overflow rather than posting new messages
out of order. Discord edits via `webhook.edit_message`, Stoat via
`Message.edit` (the bot owns its masqueraded messages). The platform's own
"(edited)" tag then appears on the relayed copies automatically.

Best-effort and silent (an untracked message, an unsupported target, a
since-deleted post, or a raising hook are all skipped). Loop-safe two ways,
like pin/role sync: each sender's edit handler drops the bridge's own relayed
copy being re-edited — Discord cache-free via the payload's `webhook_id`,
Stoat via the bot author on `event.after` — and `BridgeCoordinator` keeps a
~10s record of the edits it issued so an echo that still slips through (e.g.
Stoat's `event.after` uncached so the author can't be checked) is dropped
before it fans back out.

### Typing sync

A "someone is typing" event in a bridged channel is relayed onto every other
connector's mapped channel (`BridgeCoordinator.handle_typing` →
`ReceiverService.trigger_typing`, gated by `supports_typing` and keyed off
the same `ChannelMappingRepository` group message relay uses — no
`MessageSyncRepository` entry, no per-message id). An explicit "stopped
typing" event (`StandardTyping.active == False`, from Stoat's
`channel_stop_typing` — Discord has no such event) routes to
`ReceiverService.stop_typing` instead: each receiver cancels its keep-alive
loop; Stoat sends a final `end_typing` to clear the indicator now, Discord
(no clear-typing API) just stops re-arming it and lets its ~10s timeout
lapse. Discord ⇄ Stoat only —
**IRC has no typing concept** (`supports_typing` stays `False`).
Fire-and-forget: nothing is recorded, and there's no echo guard — the bridge
posts via webhook/masquerade (which don't emit typing events) and each
sender drops typing from its own bot user (`_handle_typing`). Best-effort and
silent (unbridged channel, unsupported target, or a raising `trigger_typing`
are all skipped). The relayed indicator is always attributed to the bridge
bot itself — neither Discord (webhook) nor Stoat (masquerade) can surface a
typing indicator under another identity, so `StandardTyping.sender_name` is
cosmetic. Both receivers run a short per-channel keep-alive loop
(re-firing the indicator every `_TYPING_REFRESH`s) that ends `_TYPING_LINGER`s
after the last event or immediately on `stop_typing`. On Stoat that loop's
end sends `end_typing`, clearing the indicator at once; Discord has no
clear-typing API, so there the loop just stops re-arming and Discord's own
~10s timeout lapses it.

### Source & pronoun forwarding

Every `StandardMessage` carries `source_label` — the origin connector's
`config.yaml` `label` ("Discord", "Stoat (public)", "IRC"), stamped by each
sender — and `sender_pronouns`, resolved best-effort by the origin sender.
Two per-connector options, both default on
(`DiscordConnectorConfig` / `StoatConnectorConfig` / `IrcConnectorConfig`,
wired to each receiver in `bridge.py`): `source_forwarding` and
`pronoun_forwarding`. A receiver whose connector has them on folds the
values into the displayed sender identity — Discord's webhook username and
Stoat's masquerade name become `name [Source, pronouns]`
(`services/formatting.decorate_sender_name`), IRC's line tag becomes
`<nick, Source, pronouns>`. Decoration runs *after* the `/link-user`
local-identity swap, so a linked sender shows their local name plus the true
origin label. On Stoat the decorated name is still clipped to the 32-char
masquerade cap; on Discord a source label containing "discord" is masked by
`_sanitize_username` (the webhook API rejects that substring).

**Pronoun resolution** is best-effort and network-fed, since neither
discord.py 2.7.1 nor stoat.py 1.2.1 models a pronoun field. A sender that
resolves does so per-user (cached ~10 min, `services/caching.AsyncTTLCache`
on `_pronoun_cache`), skips entirely when its own `pronoun_forwarding` is
off, and swallows every failure to `None`:

- **Discord**: **disabled** — `sender_pronouns` is always `None`
  (`DiscordSenderService._resolve_sender_pronouns` is a stub). The only
  source, the undocumented `GET /users/{id}/profile` REST endpoint, is
  hard-blocked for bot tokens (`403`, error code `20001` "Bots cannot use
  this endpoint"), so it failed on every relayed message (issue #58). The
  profile-fetch code is preserved commented-out for a future where Discord
  exposes pronouns to bots; a pronoun-role scan is a possible fallback, not
  yet done. Discord's `pronoun_forwarding` now only governs whether an
  *inbound* message's pronouns show in the webhook name (as on IRC).
- **Stoat** (`StoatSenderService._fetch_pronouns`): a raw
  `http.request` for the server member (`SERVERS_MEMBER_FETCH`), then the
  account user, then the user profile — first `pronouns` key wins
  (`stoat_service.formatting._extract_pronouns`, checks top-level and a
  nested `profile`). stoat.py drops unknown payload keys, so the parsed
  `User`/`Member` objects are bypassed; a deployment without a pronoun field
  just yields `None`.
- **IRC** has no pronoun concept — `sender_pronouns` is always `None` there;
  its `pronoun_forwarding` only governs whether an *inbound* message's
  pronouns show in the line tag.

### Admin & status commands

Every admin/status command (`/status`, the channel commands `/link channel` /
`/unlink channel` / `/mirror channel to` / `/mirror channel from` /
`/linked channels`, the role commands
`/link role` / `/mirror role to` / `/mirror role from` / `/linked roles` /
`/unlink role`
(Discord/Stoat only), the user commands `/link user` / `/unlink user` /
`/linked users`, the category commands `/link category` / `/unlink category` /
`/mirror category to` / `/mirror category from` / `/linked categories`
(Discord/Stoat only), the emote
commands `/link emote` / `/mirror emote to` / `/mirror emote from` /
`/linked emotes` / `/unlink emote`
(Discord/Stoat only - IRC has no custom emoji)) and how to
reach it on each connector is documented in
`COMMANDS.md`, not duplicated here. `/mirror <noun>` is a two-way group:
`to` pushes a local entity onto another connector (the historical
`/mirror <noun>` behaviour), `from <service> <external_id>` pulls a remote
entity in and creates the local copy - respecting already-linked entities
(bridge/mapping groups are reused). `/mirror channel` in *either* direction
(and `all`) lands the counterpart in the destination's own copy of the source
channel's linked Category when that Category is `/link category`-linked -
resolved by `ChannelLinker._local_category_for_source_channel`, not by an
exact Category-name match - and only falls back to a same-named Category when
it isn't linked (issue #50). When `/mirror category` *creates* the
counterpart Category, it records it (and places the source Category's child
channels) under the title it just asked `ensure_category` for, not by
re-resolving the id through the connector's name cache - that cache is
populated at connect and blind to a brand-new Category, so re-resolving
handed back the raw id, which then got stored as the Category name and
passed on as a child-channel Category title, spawning a second Category
literally named after the id (issue #64). Both directions of every
`/mirror <noun>` take an optional trailing `new_name` (`admin_commands.py`'s
`_clean_new_name`): the name the counterpart is created/matched under on the
destination instead of carrying the source name over - routed through the
destination's `ensure_*` hook so it's destination-normalised and still
get-or-creates (so a same-named existing entity is matched, not duplicated);
the way to aim `/mirror channel` at an unlinked existing destination channel,
IRC especially (issue #44). Not on the `all` fan-out; on IRC it's a trailing
`AS <new_name>`; `/mirror category`'s renames only the Category, not its
mirrored child channels. `/mirror channel` (both
directions) refuses a source channel the bridge bot can't see - gated by
`ConnectorInfo.can_view_channel` (Discord/Stoat check the bot member's
`view_channel` on the channel; IRC leaves it unset), checked in
`ChannelLinker.mirror_channel` and, for the Discord current-channel case,
also against `interaction.app_permissions` - so it never mirrors a hidden
channel into a stub named after the platform's `__hidden__` placeholder
(issue #33). Deliberately narrow: only a definite "bot lacks view here"
blocks it; a `None`/"can't tell" result doesn't. On Discord the channel,
role, user, category and emote commands are real `app_commands` subcommand
groups (`/link`, `/unlink`, `/mirror`, `/linked`) - and each `/mirror <noun>`
is itself a `to`/`from` subgroup; on Stoat they're the equivalent
`stoat.ext.commands` groups, triggered on a per-connector `command_prefix`
(`StoatConnectorConfig`, `/` by default) (`_StoatClient` subclasses `commands.Bot`; the
`_<verb>_<noun>` methods on `StoatSenderService` are what the subcommands
forward to, and `_handle_message` skips relaying anything the command
processor already claimed - tracked by message id in `_command_message_ids`,
which also covers the bot's own `_reply` output); on IRC they're
space-separated (`LINK CHANNEL …` / `LINK USER …` / `MIRROR CHANNEL TO …` /
`MIRROR CHANNEL FROM …`). No flat admin commands remain.
Every id argument to a channel, role, user, category or emote command also
accepts a bare name (`ConnectorInfo.resolve_channel_id_by_name` /
`resolve_role_id_by_name` / `resolve_user_id_by_name` /
`resolve_category_id_by_name`). On IRC `resolve_channel_id_by_name` isn't a
lookup (a channel id there *is* its name) — it just sterilizes the token
into the `#name` shape the server accepts (prepends `#`, drops characters
IRC channel names can't hold — `irc_service.formatting.normalize_channel_name`,
also used by `ensure_channel`), so `/link channel irc general` works the
same as `/link channel irc #general` (issue #41). That same helper is wired
as `ConnectorInfo.normalize_channel_name` and applied by
`ChannelLinker.link_channel` to a mirrored channel's stored *name*, so
`/mirror channel to irc danksquad` records name `#danksquad` to match the
`#danksquad` id `ensure_channel` returned rather than a bare `danksquad`
(issue #51). On Discord those id options are also
**autocompleted**: the `external_id` option lists the real
channels/roles/users/Categories/emoji on whatever connector the `service`
option names, and `local_id` lists this guild's own — pulled live from the
target `ConnectorInfo.list_channels` / `list_categories` / `list_roles` /
`list_users` / `list_emotes` hooks (Discord + Stoat implement them off their
cached guild/server; IRC wires only `list_channels`, from the channels it
already knows — config plus anything linked — and leaves the rest unset).
`_entity_autocomplete_choices`
(`services/discord_service/commands.py`) is the shared filter, the entity-id
counterpart of `_connector_autocomplete_choices`; every lookup is
best-effort (an un-picked `service`, an unset or raising hook, or a
disconnected client all just yield an empty menu and the option still takes
a hand-typed id/name). Shared logic
lives in `admin_commands.py` (`ChannelLinker` / `CategoryLinker` /
`EmoteLinker` / `UserLinker` / `RoleLinker`), called
identically from each connector's own `services/*.py` module. Nothing is
bridged (or mention-linked) automatically — every pair is linked explicitly
via those commands.

When `/mirror channel` (or `/mirror channel from`, thread auto-mirror, or
linked-Category auto-sync) **creates** a counterpart channel, it carries the
source channel's cosmetic metadata over so the new channel isn't left blank
(`models.ChannelMetadata` — description, NSFW/maturity flag, icon URL). The
source connector's `ConnectorInfo.describe_channel` hook reads it, and it's
passed to the destination's `ensure_channel` as a `metadata=` keyword which
each hook applies **only on the create path** — a mirror that reuses/matches
an existing channel leaves its metadata untouched. Discord (topic + NSFW; no
per-channel icon) and Stoat (description + NSFW + icon, the icon a
best-effort `channel.edit` after create) both implement `describe_channel`
and channel creation; IRC's `ensure_channel` sets only the channel TOPIC from
`description` (and only when its JOIN just created the channel — the server
auto-ops the first joiner), leaves `describe_channel` unset, and ignores
NSFW/icon. All best-effort — a missing or raising `describe_channel` just
means no metadata is carried.

`RoleLinker` (`storage/role_mappings.py`) is the role-level counterpart of
`ChannelLinker`. Every id argument also accepts a bare role name via each
connector's `resolve_role_id_by_name` hook; `/mirror role` creates-or-matches
a same-named role via the `ensure_role` hook (name only — color/permissions
aren't copied). Linked-role `<@&id>` / `<%id>` mentions are rewritten into the
target's linked role (`@Name` on IRC) alongside the user/channel mention
rewrites.

A user `<@id>` mention is rewritten to the target's native mention of the
`/link-user`-linked identity where one exists; where it doesn't,
`rewrite_mentions` expands it to a plain `@Display Name` (the mentioned
user's name on the origin, carried on `StandardMessage.mentioned_users` —
populated best-effort by the Discord/Stoat senders off the message's
`mentions`, absent on IRC which has no structured mentions) rather than
relaying the raw `<@id>` token (issue #56). A mention the map can't name is
still left exactly as it appeared. That expansion is the one place relayed
text picks up an `@`-prefixed token from an attacker-controlled string, so
it's run through `mentions._defang_mentions` (a zero-width space wedged in
after the sigil of any `@everyone` / `@here` / `<@…>` / `<#…>` / `<%…>` it
contains) — the bridge sets no `allowed_mentions` on its webhook/masquerade
sends, so an un-defanged `@everyone` in a display name would be a live mass
ping. It's also applied *after* the plain-word nick scan so an injected
name can't itself be re-read as a nick mention.

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
conservative subset — only bits that mean the same on both platforms — but
the discord.py/stoat.py flag names on both sides of that subset are verified
against each library's `Permissions` flag class (discord.py 2.7.1 /
stoat.py 1.2.1) and pinned there by
`tests/test_stoat_permission_flag_names.py`. The Stoat command-execution
gate (`StoatSenderService._is_admin`) likewise checks the real
`Permissions.manage_server` flag (server owners always pass). stoat.py's
member/role/channel gateway *event shapes* — `ServerMemberUpdateEvent`,
`RawServerRoleUpdateEvent`, `ServerRoleDeleteEvent`, `ChannelUpdateEvent`,
including the `event_name`→`on_*` handler mapping — are verified against
stoat.py 1.2.1 (`stoat.events`); only live-server payload completeness
(whether `before`/`after` arrive populated, which depends on cache state)
is still unverified.

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

Category linking (`/link category`) is Discord/Stoat-only (IRC has no
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
`(connector, parent_channel_id)`), which `/link category` checks to refuse ever
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
  channel_structure.py         # clip_name helper for fitting names into Stoat's 32-char channel-name limit
  admin_commands.py            # ChannelLinker / CategoryLinker / EmoteLinker / UserLinker / RoleLinker - shared linking logic
  bridge.py                    # BridgeCoordinator: routes StandardMessages sender -> receiver via channel mappings
  status.py                    # HealthTracker: per-connector sync target health, read by the /status commands
  services/
    base.py                    # SenderService / ReceiverService base classes
    formatting.py               # content formatting utilities shared across receivers
    discord_service/            # Discord sender (client) + receiver (per-channel webhook), one instance per config.yaml entry
    stoat_service/              # Stoat sender/receiver, one instance per config.yaml entry
    irc_service/                # IRC sender/receiver, one instance per config.yaml entry
      # each *_service/ package splits its connector by area of concern:
      #   client.py    - native event -> owner shim
      #   commands.py  - command parsing (the /link etc. tree / DM dispatch)
      #   linking.py   - Mongo-backed /link /unlink /linked /mirror handlers
      #   lookups.py   - platform-resource lookups (id<->name, get-or-create)
      #   sync.py      - reaction / role / emoji / pin / typing sync handlers
      #   formatting.py- network-free conversion helpers
      #   sender.py / receiver.py - the composed *SenderService / *ReceiverService
      # linking/lookups/sync are mixins composed into *SenderService. irc_service/
      # is lighter (client / commands / formatting / sender / receiver only).
      # stoat_service/_compat.py runtime-patches a stoat.py 1.2.1 command-framework
      # bug (issue #40): its Command.transform/.signature call issubclass() on a
      # parameter's raw annotation, which raises TypeError for any Optional[...] /
      # Union[...] arg - i.e. most of the /link /unlink /linked /mirror tree.
      # apply_stoat_command_patches() (called at import of stoat_service/commands.py)
      # wraps that issubclass so a non-class first arg returns False, as discord.py does.
  storage/
    mongo.py                   # MongoDB connection (motor)
    channel_mappings.py        # which channels, across connectors, are bridged together
    category_mappings.py       # which categories, across connectors, are bridged together; ThreadCategoryRepository binds a thread's parent channel to its (unlinkable) thread-only category id
    role_mappings.py           # which roles, across connectors, are linked (Discord/Stoat only); backs /link role, role-mention rewriting, and auto-grant
    message_sync.py            # cross-connector message ID references, for edit/delete sync
    emoji_mappings.py          # cross-connector custom emoji ID references, for reaction sync
  services/role_sync.py        # network-free helpers for the role permission-mirror flow (neutral<->native permission translation)
```
