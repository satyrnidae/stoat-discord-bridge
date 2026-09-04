# stoat-discord-bridge

![CI](https://github.com/satyrnidae/stoat-discord-bridge/actions/workflows/ci.yml/badge.svg)

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
priority when both are set. A `{SECTION}__{index}__{FIELD}_FILE` env var
naming a file (Docker/Kubernetes secrets convention) is a third source, and
any resolved value may be a 1Password secret reference
(`op://<vault>/<item>/<field>`), dereferenced via the `op` CLI at startup if
present. See `src/stoat_discord_bridge/config.py`'s docstring for the full
rules. `config.yaml` itself is gitignored - copy it
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

The image ships the 1Password `op` CLI, so `op://<vault>/<item>/<field>`
values in `config.yaml` resolve at startup - authenticate it with a
service-account token, mounted as a Docker secret and pointed at by
`OP_SERVICE_ACCOUNT_TOKEN_FILE` (see the commented block in
`docker-compose.yml` and the example in `docker-compose.override.yml`).
Build with `--build-arg INSTALL_OP=0` to leave `op` out.

Log output (and hence `docker logs`) is redacted: bot tokens, the IRC
NickServ/OPER passwords, and any credentials embedded in the Mongo URI are
replaced with `***` wherever they'd otherwise appear, as are the passwords
in raw IRC `OPER` / `PASS` / `NickServ IDENTIFY` protocol lines. Set
`LOG_REDACT_IDS=1` to additionally redact raw platform ids (Discord
snowflakes, Stoat ULIDs) - off by default since ids aren't credentials and
redacting them makes debugging harder. See
`src/stoat_discord_bridge/logging_setup.py`.

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

Attachments arrive as URLs. The Discord and Stoat receivers re-upload each
one as a native file on the relayed message rather than pasting the (signed,
expiring) CDN link into the text; anything over 8 MiB or that can't be
fetched falls back to an inlined URL. IRC, which has no native attachment
concept, always inlines the URL.

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
- a Stoat reaction using one of Stoat's *builtin* (non-Unicode, non-custom)
  emoji — the `distorted_face` / `trollface` pack — has no equivalent on
  other connectors and is dropped toward all of them

The bridge adds its mirrored reaction once — a second user reacting with the
same emoji on the origin is a no-op — and holds it until the **last** origin
user removes theirs (tracked by reactor count on the origin message; the
receiver hooks are independently idempotent as a backstop).

Deleting a custom emoji is **never** mirrored onto other connectors — a copy
still in use elsewhere keeps working. Deleting it only updates
`EmojiMappingRepository`'s bookkeeping for the connector it was deleted on
(`forget()`); the cross-connector mapping itself is only dropped once every
connector's copy has been deleted.

IRC has no reaction or custom-emoji concept, so it's excluded from both —
`ReceiverService.supports_reactions` / `supports_emoji` gate this per
connector, and IRC's receiver leaves them at the base-class default (`False`).

## Typing sync

When someone starts typing in a bridged channel, a typing indicator is shown
in every other connector's mapped channel (`supports_typing` /
`trigger_typing`, Discord ⇄ Stoat only — IRC has no typing concept).
Fire-and-forget and best-effort: nothing is tracked, no per-message id is
involved, and an unbridged channel or a transient failure is silently
skipped. The indicator always shows as the bridge bot — neither a Discord
webhook nor a Stoat masquerade can attribute typing to the origin user.

## Message edit sync

Editing a message's text in a bridged channel edits every other connector's
relayed copy in place (`supports_edits` / `edit_message`, keyed off the same
cross-connector message-id tracking reaction sync uses — Discord ⇄ Stoat
only, IRC has no edit-in-place). The new text is re-rendered through the same
mention/emoji rewrites the first relay used; a relay that was split across
several posts is matched chunk-for-post. Best-effort and silent, and
loop-safe two ways: a bot-authored edit (the bridge's own webhook/masquerade
message) is ignored, and the coordinator briefly records the edits it issued
so the resulting echo is dropped. The platform's own "(edited)" marker then
appears on the copies automatically.

## Commands

Every admin/status command (`/status`, `/link channel`, `/linked channels`,
`/mirror channel`, `/unlink channel`, `/link role`, `/link user`,
`/linked users`, `/link emote`) and how to reach it on
each connector is documented in [`COMMANDS.md`](COMMANDS.md).

## Contributing

- **Branch names** group by issue type under a folder-like prefix:
  `bug/*`, `feat/*`, `chore/*` (e.g. `bug/94-thread-parent-group`,
  `feat/81-mirror-full-refresh`, `chore/american-spelling`).
- **Commit messages** follow the [Gitmoji](https://gitmoji.dev) convention —
  lead the summary line with the emoji matching the change's type (`✨` new
  feature, `🐛` bug fix, `♻️` refactor, `📝` docs, `✅` tests, `🔀` merge,
  etc.).

## Layout

```
config.yaml                 # every configured connector (Discord/Stoat/IRC) - no secrets
src/stoat_discord_bridge/
  config.py                 # loads config.yaml + resolves secrets named by it from .env
  models.py                  # StandardMessage — the platform-neutral message format
  channel_structure.py        # clip_name helper for fitting names into Stoat's 32-char channel-name limit
  admin_commands.py            # ChannelLinker / CategoryLinker / EmoteLinker / UserLinker / RoleLinker - shared linking logic
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
    message_sync.py            # cross-connector message ID references, for edit sync (and future delete sync)
    emoji_mappings.py           # cross-connector custom emoji ID references, for reaction sync
```

Status: scaffolding only — client connections work for Discord/Stoat
senders, and the Discord and Stoat receivers post via webhook/masquerade
respectively; IRC's receiver and its asyncio integration are implemented but
unverified against a live server. Reaction and custom-emoji sync
(`services/discord_service.py`, `services/stoat_service.py`, `bridge.py`) is
wired to `stoat.py`'s real event/method names
(`on_message_react`/`on_message_unreact`,
`on_server_emoji_create`/`on_server_emoji_delete`, `Message.react`/`unreact`,
`Message.reactions`) as verified against the installed package, but the
end-to-end flow still needs a manual check against a live Stoat + Discord
deployment — same caveat as the rest of the Stoat integration and the
WHOIS-based IRC-operator check backing IRC's admin DM commands' permission
gate.
