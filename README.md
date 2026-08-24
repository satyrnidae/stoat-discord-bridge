# stoat-discord-bridge

Multi-way chat bridge: **Discord** ↔ **Stoat (public)** ↔ **Stoat (self-hosted)** ↔ **IRC**.

Incoming messages are relayed into Discord "as" the originating Stoat/IRC user via
per-channel Discord webhooks (username + avatar override), rather than posting under
the bridge bot's own identity.

## Endpoints

- Discord guild: configured via `DISCORD_GUILD_ID`
- Stoat (public instance): server configured via `STOAT_PUBLIC_SERVER_ID`
- Stoat (self-hosted, srv.satyrn.dev): server configured via `STOAT_SELFHOSTED_SERVER_ID`
- IRC: `irc.satyrn.dev` (Tethys IRCd)

## Setup

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -e .
copy .env.example .env
# fill in .env with real bot tokens, then:
python -m stoat_discord_bridge
```

## Architecture

Service-based: each endpoint (Discord, Stoat public, Stoat self-hosted, IRC)
has a **sender** service (listens to the platform, turns native events into a
standardized message) and a **receiver** service (takes a standardized
message and posts it into the platform). `BridgeCoordinator` (`bridge.py`)
wires every sender's output to every other platform's receiver, looking up
which channels are bridged together via MongoDB.

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

Discord and Stoat (public + self-hosted) reactions are mirrored onto every
other platform's copy of the same message (via `MessageSyncRepository`,
which already tracks cross-platform message IDs); custom emoji created on
one of those platforms are mirrored onto the others so a reaction using
them can be recreated at all. Both directions are best-effort and silently
skip rather than error:

- a reaction on a message the bridge never relayed (unbridged channel, or
  posted before the bridge saw it) is dropped
- a custom emoji that a target platform can't create (emoji slots full,
  rejected name, oversized image, etc.) is skipped on that platform only —
  every other target still gets it
- a reaction using a custom emoji that was never successfully mirrored onto
  a given target (including the "couldn't create it" case above) is
  ignored for that target

Deleting a custom emoji is **never** mirrored onto other platforms — a copy
still in use elsewhere keeps working. Deleting it only updates
`EmojiMappingRepository`'s bookkeeping for the platform it was deleted on
(`forget()`); the cross-platform mapping itself is only dropped once every
platform's copy has been deleted.

IRC has no reaction or custom-emoji concept, so it's excluded from both —
`ReceiverService.supports_reactions` / `supports_emoji` gate this per
platform, and IRC's receiver leaves them at the base-class default (`False`).

## Status command

Each endpoint exposes a way to check sync target health (`healthy` /
`degraded` / `failing` per platform, tracked in `status.py`'s
`HealthTracker` from sender connection state and recent relay outcomes):

- Discord: `/status` slash command
- Stoat: `/status` message command
- IRC: `STATUS`, sent as a DM to the bot

## Mirror-channels command

`/mirror-channels`, sent as a message in Stoat by someone with Manage Server
permission, recreates the bridged Discord guild's current category/channel
layout on that Stoat server (see `channel_structure.py` and
`services/stoat_service.py`). It's additive/idempotent — existing
categories/channels (matched by name) are left alone, nothing is deleted or
renamed.

Discord forum channels have no Stoat equivalent, so each forum is mirrored
as its own group named after the forum, containing one channel per
currently active post in it (archived posts aren't included). This command
only creates matching structure — it does not bridge the new channels to
their Discord counterparts; that still requires a manual
`ChannelMappingRepository.upsert()`.

## Layout

```
src/stoat_discord_bridge/
  config.py              # env-backed config (tokens, Mongo URI) + known server/guild IDs
  models.py               # StandardMessage — the platform-neutral message format
  channel_structure.py     # GuildStructure snapshot used by the /mirror-channels command
  bridge.py                # BridgeCoordinator: routes StandardMessages sender -> receiver via channel mappings
  status.py                # HealthTracker: per-platform sync target health, read by the /status commands
  services/
    base.py                 # SenderService / ReceiverService base classes
    discord_service.py       # Discord sender (client) + receiver (per-channel webhook)
    stoat_service.py          # Stoat sender/receiver (instantiated per server: public + self-hosted)
    irc_service.py             # IRC sender/receiver
  storage/
    mongo.py                 # MongoDB connection (motor)
    channel_mappings.py       # which channels, across platforms, are bridged together
    message_sync.py            # cross-platform message ID references, for future edit/delete sync
    emoji_mappings.py           # cross-platform custom emoji ID references, for reaction sync
```

Status: scaffolding only — client connections work for Discord/Stoat
senders, and the Discord and Stoat receivers post via webhook/masquerade
respectively; IRC's receiver, its asyncio integration, and channel-mapping
seeding are stubbed with `TODO`s (see `services/irc_service.py`). Reaction
and custom-emoji sync (`services/discord_service.py`,
`services/stoat_service.py`, `bridge.py`) is implemented against a best
guess at `stoat.py`'s event names and object shape, flagged with `TODO`s
where unconfirmed — same caveat as the rest of the Stoat integration.
