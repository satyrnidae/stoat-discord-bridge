# stoat-discord-bridge

Multi-way chat bridge across any number of **Discord**, **Stoat**, and **IRC**
servers, configured entirely in `config.yaml` (no code changes needed to add
another server).

Incoming messages are relayed into Discord "as" the originating Stoat/IRC user
via per-channel Discord webhooks (username + avatar override, created/looked-up
automatically), rather than posting under the bridge bot's own identity.

## Connectors

`config.yaml` lists every connector the bridge runs, grouped by kind
(`discord` / `stoat` / `irc`). Each entry has a unique `id` (used as the
`<source>` argument to the admin commands below). Any other field on a
connector - tokens, guild/server ids, IRC credentials, whatever - can be a
literal value in `config.yaml` or an `{SECTION}__{index}__{FIELD}` env var
(Azure App Configuration/ASP.NET Core-style hierarchical binding, `index`
being the connector's 0-based position in its kind's list - e.g.
`STOAT__1__TOKEN` for the 2nd `stoat:` entry), with the env var taking
priority when both are set. See `src/stoat_discord_bridge/config.py`'s
docstring for the full rules. `config.yaml` itself is gitignored - copy it
from `config.yaml.example` (which documents every field) and fill in your
actual deployment's ids/hosts. Adding another server of any kind is just
another list entry (literal, env-backed, or both).

## Setup

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -e .
copy .env.example .env
copy config.yaml.example config.yaml
# fill in .env with real bot tokens, fill in config.yaml with your actual
# deployment's ids/hosts, then:
python -m stoat_discord_bridge
```

## Docker

```powershell
copy .env.example .env
copy config.yaml.example config.yaml
# fill in both as above (MONGO__URI in .env can be left as-is or omitted -
# docker-compose.yml always points the bridge at the mongo container
# below, overriding whatever .env says), then:
docker compose up --build
```

Runs the bridge alongside a MongoDB instance (`mongo:7`, data persisted in
the `mongo-data` named volume) - no separate Mongo setup needed.
`config.yaml` and `certs/` are bind-mounted read-only into the container
(so editing either doesn't require a rebuild, just a restart); `.env` is
loaded via `env_file`. Neither secrets nor `config.yaml` are baked into
the image (see `.dockerignore`).

The bridge image ships a `HEALTHCHECK` (`docker ps` / `docker inspect` shows
it) polling a liveness-only `GET /healthz` on port 8080
(`src/stoat_discord_bridge/health_server.py`) - it proves the event loop is
responsive, not that every connector is currently connected (a transient IRC
reconnect shouldn't flip the container unhealthy and get it restarted,
killing the other, still-fine connectors with it). Per-connector state is
still available via `/status` (Discord/Stoat) and `STATUS` (IRC DM), plus a
`GET /status` JSON endpoint on the same port mirroring the same data.

## Tests

```powershell
pip install -e ".[test]"
pytest
```

Covers the pure-logic layer (config resolution, the storage repositories
against an in-memory fake Mongo, the admin-command linkers, mention
rewriting) plus a few network-free pieces of the service modules. Doesn't
cover live Discord/Stoat/IRC connectivity - see CLAUDE.md for the exact
scope.

## Architecture

Service-based: each configured connector gets a **sender** service (listens
to it, turns native events into a standardized message) and a **receiver**
service (takes a standardized message and posts it into that connector).
`BridgeCoordinator` (`bridge.py`) wires every sender's output to every other
connector's receiver, looking up which channels are bridged together via
MongoDB.

Messages move between services as a `StandardMessage`
(`models.py`) — a platform-neutral shape with:

- sender profile image (or null)
- channel name
- sender display name / username / nickname
- message content in Markdown
- attachment data / URLs
- a message ID for sync tracking

Platform-specific particularities (stripping Markdown, inlining attachment
URLs, splitting long messages for IRC, etc.) are handled inside each
receiver's `receive()`, not in the shared message format.

## Reaction & custom emoji sync

Discord and Stoat reactions are mirrored onto every other connector's copy
of the same message (via `MessageSyncRepository`, which already tracks
cross-connector message IDs); custom emoji created on one of those
connectors are mirrored onto the others so a reaction using them can be
recreated at all. Both directions are best-effort and silently skip rather
than error:

- a reaction on a message the bridge never relayed (unbridged channel, or
  posted before the bridge saw it) is dropped
- a custom emoji that a target connector can't create (emoji slots full,
  rejected name, oversized image, etc.) is skipped on that connector only —
  every other target still gets it
- a reaction using a custom emoji that was never successfully mirrored onto
  a given target (including the "couldn't create it" case above) is
  ignored for that target

Deleting a custom emoji is **never** mirrored onto other connectors — a copy
still in use elsewhere keeps working. Deleting it only updates
`EmojiMappingRepository`'s bookkeeping for the connector it was deleted on
(`forget()`); the cross-connector mapping itself is only dropped once every
connector's copy has been deleted.

IRC has no reaction or custom-emoji concept, so it's excluded from both —
`ReceiverService.supports_reactions` / `supports_emoji` gate this per
connector, and IRC's receiver leaves them at the base-class default (`False`).

## Status command

Each connector exposes a way to check sync target health (`healthy` /
`degraded` / `failing` per connector, tracked in `status.py`'s
`HealthTracker` from sender connection state and recent relay outcomes):

- Discord: `/status` slash command
- Stoat: `/status` message command
- IRC: `STATUS`, sent as a DM to the bot

## Channel linking

Nothing is bridged automatically — every pair of channels has to be linked
explicitly, via one of:

### `/link-channel <source> <source_id> [<destination_id>]`

Links `source_id` on connector `<source>` (any configured connector id) to
`<destination_id>` on the connector the command is run on — or to the
current channel if `<destination_id>` is omitted. If either channel is
already linked, the existing bridge group is reused; if *both* are already
linked to two different groups, the command fails rather than merging them
(unlink one side first). Available on:

- **Discord**: `/link-channel` slash command (requires Manage Server)
- **Stoat**: `/link-channel <source> <source_id> [<destination_id>]` message
  command (requires Manage Server)
- **IRC**: `!link-channel <source> <source_id> [<destination_id>]`, sent as a
  channel message (requires channel-operator status). IRC uses `!` instead
  of `/` here since many clients treat a leading `/` as a local command and
  never send it as text.

### `/mirror-channels <source>`

Sent as a message in Stoat by someone with Manage Server permission,
recreates `<source>`'s (a configured Discord connector's) current
category/channel layout on that Stoat server (see `channel_structure.py`
and `services/stoat_service.py`) — additive/idempotent, existing
categories/channels (matched by name) are left alone, nothing is deleted or
renamed. Unlike `/link-channel`, this is Stoat-only on the receiving side:
Discord doesn't need to mirror structure onto itself, and IRC networks
don't offer bot-driven channel creation.

Every channel it creates **or matches by name** is also linked back to its
Discord counterpart (same underlying logic as `/link-channel`) — so
`/mirror-channels` alone is enough to both create and bridge a Stoat
server's structure from Discord. A channel that's already linked to a
*different* group is skipped (reported in the summary), not overwritten.

Discord forum channels have no Stoat equivalent, so each forum is mirrored
as its own group named after the forum, containing one channel per
currently active post in it (archived posts aren't included).

## Layout

```
config.yaml                 # every configured connector (Discord/Stoat/IRC) - no secrets
src/stoat_discord_bridge/
  config.py                 # loads config.yaml + resolves secrets named by it from .env
  models.py                  # StandardMessage — the platform-neutral message format
  channel_structure.py        # GuildStructure snapshot used by the /mirror-channels command
  admin_commands.py            # ChannelLinker / StructureMirrorer - shared /link-channel & /mirror-channels logic
  bridge.py                     # BridgeCoordinator: routes StandardMessages sender -> receiver via channel mappings
  status.py                      # HealthTracker: per-connector sync target health, read by the /status commands
  services/
    base.py                 # SenderService / ReceiverService base classes
    discord_service.py       # Discord sender (client) + receiver (per-channel webhook), one instance per config.yaml entry
    stoat_service.py          # Stoat sender/receiver, one instance per config.yaml entry
    irc_service.py             # IRC sender/receiver, one instance per config.yaml entry
  storage/
    mongo.py                 # MongoDB connection (motor)
    channel_mappings.py       # which channels, across connectors, are bridged together
    message_sync.py            # cross-connector message ID references, for future edit/delete sync
    emoji_mappings.py           # cross-connector custom emoji ID references, for reaction sync
```

Status: scaffolding only — client connections work for Discord/Stoat
senders, and the Discord and Stoat receivers post via webhook/masquerade
respectively; IRC's receiver and its asyncio integration are implemented but
unverified against a live server. Reaction and custom-emoji sync
(`services/discord_service.py`, `services/stoat_service.py`, `bridge.py`) is
implemented against a best guess at `stoat.py`'s event names and object
shape, flagged with `TODO`s where unconfirmed — same caveat as the rest of
the Stoat integration and the IRC channel-operator check backing
`!link-channel`'s permission gate.
