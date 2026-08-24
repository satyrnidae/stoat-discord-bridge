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
IRC channel-operator check backing `!link-channel`'s permission gate.

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
`config.yaml`. This means any field — not just tokens — can live in
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

### Status command

Each connector exposes a way to check sync target health (`healthy` /
`degraded` / `failing` per connector, tracked in `status.py`'s
`HealthTracker` from sender connection state and recent relay outcomes):
Discord `/status` slash command, Stoat `/status` message command, IRC
`STATUS` sent as a DM to the bot.

### Channel linking (`admin_commands.py`)

Nothing is bridged automatically — every pair of channels must be linked
explicitly:

- **`/link-channel <source> <source_id> [<destination_id>]`** — links
  `source_id` on connector `<source>` to `<destination_id>` on the connector
  the command is run on (or the current channel if omitted). If either side
  is already linked, the existing bridge group is reused; if *both* sides are
  already linked to *different* groups, the command fails rather than merging
  them (unlink one side first). Available as a Discord slash command
  (Manage Server), a Stoat message command (Manage Server), and IRC's
  `!link-channel` channel message (channel-operator status) — IRC uses `!`
  instead of `/` since many clients treat a leading `/` as a local command.
- **`/mirror-channels <source>`** — Stoat-only message command (Manage
  Server) that recreates a configured Discord connector's current
  category/channel layout on that Stoat server (`channel_structure.py`).
  Additive/idempotent: existing categories/channels matched by name are left
  alone, nothing deleted or renamed. Every channel it creates or matches is
  also linked back to its Discord counterpart, so this command alone both
  creates and bridges a Stoat server's structure from Discord. A channel
  already linked to a *different* group is skipped (reported in the
  summary), not overwritten. Discord forum channels have no Stoat
  equivalent, so each forum mirrors as its own group named after the forum,
  with one channel per currently active (non-archived) post.

### Discord threads

Discord threads have no IRC/Stoat equivalent. Design intent: treat a Discord
thread as a new Stoat channel under a "Threads" category rather than trying
to map it onto IRC/Stoat's flat channel model.

## Layout

```
config.yaml                    # every configured connector (Discord/Stoat/IRC) - gitignored, see config.yaml.example
src/stoat_discord_bridge/
  config.py                    # loads config.yaml, layering env vars over it per-field (see its docstring)
  models.py                    # StandardMessage - the platform-neutral message format
  channel_structure.py         # GuildStructure snapshot used by the /mirror-channels command
  admin_commands.py            # ChannelLinker / StructureMirrorer - shared /link-channel & /mirror-channels logic
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
    message_sync.py            # cross-connector message ID references, for edit/delete sync
    emoji_mappings.py          # cross-connector custom emoji ID references, for reaction sync
```
