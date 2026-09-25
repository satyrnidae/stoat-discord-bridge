# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

> [!IMPORTANT]
> **Use American spelling** everywhere — identifiers, comments, docstrings,
> log messages, documentation, and commit messages. Write `color`, `behavior`,
> `normalize`, `recognize`, `honor`, `canceled`, etc., not their British
> forms. (Python/library names that are themselves British — `asyncio`'s
> `CancelledError`, discord.py's `Colour` — stay as the library spells them;
> discord.py's `color`/`colour` aliases, prefer `color`.)

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
`admin_commands`'s linkers, `services/mentions.py`, and a few
network-free pieces of the service modules (IRC's WHOIS-based oper check,
Stoat's websocket-gateway discovery). Does not cover the actual
Discord/Stoat/IRC network integration - no live-server or full-client-mock
tests exist yet, so those still need manual verification. No linter config
in this repo yet; CI (`.github/workflows/ci.yml`) runs `pytest` on Python
3.11-3.13 plus an sdist/wheel build on every push to `main` and every PR.
`tests/` mirrors the source layout: a suite that outgrew one module
(admin_commands' linkers, the Stoat/Discord admin-command dispatch surfaces,
the Discord/Stoat receivers, the Discord sender's gateway-event dispatch,
IRC's connection-free surface, `BridgeCoordinator`) is a same-named package
instead - one file per concern plus a `conftest.py` for the fakes/fixtures
that concern's files share (`tests/admin_commands/`, `tests/stoat_service/`,
`tests/discord_service/`, `tests/discord_receiver/`, `tests/stoat_receiver/`,
`tests/discord_sender_dispatch/`, `tests/irc_service/`, `tests/bridge/`).
`tests/fakes/fake_linkers.py` holds the one `ChannelLinker`/`UserLinker`
double pair actually shared verbatim across connectors (Stoat and IRC's
admin-dispatch suites); a fake shape specific to one connector or one
package stays in that package's own `conftest.py` instead.

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

## Contributing

Branch names group by issue type under a folder-like prefix - `bug/*`,
`feat/*`, `chore/*` (e.g. `bug/94-thread-parent-group`,
`feat/81-mirror-full-refresh`, `chore/american-spelling`) - so use that
shape for any branch created on this repo's behalf. Commit messages follow
the [Gitmoji](https://gitmoji.dev) convention: lead the summary line with
the emoji matching the change's type (`✨` new feature, `🐛` bug fix, `♻️`
refactor, `📝` docs, `✅` tests, `🔀` merge, etc.).

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

A link preview (an auto-unfurled embed) is relayed as the *source*
platform's own preview media, re-uploaded as an attachment, rather than
letting each destination unfurl the link itself (issue #164, generalizing
#102's GIF-picker case — a Discord GIF-picker pick is just a Tenor/Klipy link
whose embed carries the media). Each sender turns every embed with fetchable
media into an `Attachment` with `source_page_url` set to the previewed link:
Discord's `discord_service/formatting._link_preview_attachments` (video if
it's a `gifv` or a media file — a YouTube-style `video.url` is a player page
— then `image`, then `thumbnail`), Stoat's
`stoat_service/formatting._link_preview_embed_attachments` (`WebsiteEmbed`
video-if-media-file then image, keyed by `original_url`; a bare
`ImageEmbed`/`VideoEmbed` points at itself). Senders leave the link in
`content_markdown`; each receiver decides via
`services/formatting.partition_link_preview_attachments`: by default it keeps
the attachment and strips the link (whole-token match only). An
`/attachments prefer <discord|stoat> <url-substr>` rule
(`admin_commands/attachment_preferences.AttachmentPreferenceManager`, Mongo
`storage/attachment_preferences.py`, longest case-insensitive substring
wins, cached ~30s) naming the receiver's own kind makes it drop the
attachment and keep the link, so its own platform unfurls it. IRC
(`my_kind=None`) always keeps the link and drops the preview media, since
the page link is more useful there than a bare media URL. A preview whose
link isn't in the text (a bot's rich embed) is always kept. Rules are keyed
by kind, not connector id. **Unverified against a live server**: whether
either platform's embed is on the message's initial payload (vs. arriving
later via an update event the senders ignore as an auto-embed unfurl) and
whether Stoat renders a re-uploaded `.mp4` inline the same way it does a
`.gif`.

A Discord **forwarded message** carries its actual content in a separate
`Message.message_snapshots` field, not in `.content`/`.attachments` — those
hold only the forwarder's own caption, if any (discord.py 2.7.1's
`MessageSnapshot`, confirmed against the installed package). Discord's
`_to_standard_message` (`services/discord_service/formatting.py`) combines
the caption with every snapshot's content as a Markdown blockquote (`>
<line>`, each line prefixed individually) and appends every snapshot's
attachments to the message's own — issue #125; without this, a forward with
no caption relayed nothing, and one with a caption relayed only the caption,
in both cases silently dropping the forwarded content itself. A forwarded
embed is still dropped - only the message's own embeds are read as link
previews (above).

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
`ReceiverService.set_pinned`, gated by `supports_pins`). **One-way**: only a
pin/unpin performed on the sync group's recorded *origin* message propagates —
`handle_pin` looks the group up via `MessageSyncRepository.find_group_if_origin`
(unlike reaction/edit sync, which stay on the any-side `find_group`), so a
pin/unpin performed directly on a *relayed copy* stays local to that platform
and is not mirrored back to the origin or across to other copies (issue #134).
Discord ⇄ Stoat only — **IRC has no message-pin concept** (`supports_pins`
stays `False`), so a pin never routes to it. Best-effort and silent (an
untracked message, a pin on a relayed copy, a missing `set_pinned` hook, or a
raising one are all skipped); loop-safe the same two ways as role sync —
`set_pinned` is idempotent (no-op if already in that state) and the
coordinator keeps a ~10s record of writes it issued so the echo event is
dropped.

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

### Message delete sync

Deleting a message in a bridged channel is mirrored onto every other
connector's copy of that message (`BridgeCoordinator.handle_delete` →
`ReceiverService.delete_message`, gated by `supports_deletes` and keyed off
the same `MessageSyncRepository` group reaction/pin/edit sync use). Discord ⇄
Stoat only — **IRC has no delete-in-place concept** (`supports_deletes` stays
`False`), so a delete never routes to it (issue #133).

Unlike edit sync's symmetric cascade (safe there because the bridge bot owns
every relayed copy, so only a real edit of the origin's own content ever
arrives), deletion doesn't have that property: a moderator with
`manage_messages` can delete *any* message in a channel, including the
bridge's own relayed copies. `handle_delete` therefore looks the group up via
`MessageSyncRepository.find_group_if_origin` (the same one-way lookup pin
sync uses, issue #134) rather than the any-side `find_group`, so a delete
reported for a relayed copy is a no-op rather than cascading back to the
source or other mirrors.

Each sender emits a `StandardDelete` (identity only — no content is needed to
delete something): Discord from `on_raw_message_delete` /
`on_raw_bulk_message_delete` (`RawMessageDeleteEvent` / `RawBulkMessageDeleteEvent`
— bulk emits one `StandardDelete` per id); Stoat from `on_message_delete` /
`on_message_delete_bulk` (`stoat.events.MessageDeleteEvent` /
`MessageDeleteBulkEvent`). A relay split across several native posts in one
channel is deleted post-by-post via `delete_message`'s `target_message_ids`
list — Discord via `webhook.delete_message`, Stoat via `Message.delete` (the
bot owns its masqueraded messages) — best-effort per id, so one post that's
already gone doesn't stop the rest of the batch.

Best-effort and silent (an untracked message, a delete reported for a
relayed copy rather than the origin, an unsupported target, or a raising hook
are all skipped). Loop-safe two ways, like pin/edit sync: each sender drops
its own relayed copy being deleted where
it can tell — Discord's `RAW_MESSAGE_DELETE` payload carries no `webhook_id`
(unlike `MESSAGE_UPDATE`), so this is only a best-effort `cached_message`
check there; Stoat checks the cached `event.message`'s author against the
bot's own id, the same way edit sync does — and `BridgeCoordinator` keeps a
~10s record of the deletes it issued so an echo that slips past the sender-side
check (the uncached case on either connector) is dropped before it fans back
out.

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

### Message reply sync

Replying to a message in a bridged channel is mirrored as a native reply onto
the target's own copy of the replied-to message, where the target platform
supports it (`ReceiverService.supports_replies`, resolved via the same
`MessageSyncRepository` pin/edit/typing sync use — issue #101). Every sender
stamps `StandardMessage.reply_to_message_id` with the *origin* connector's own
id of the message being replied to (`None` if it isn't a reply); unlike pin/
edit sync this needs no echo guard or `_recent_*` bookkeeping, since it's an
attribute read off a message already flowing one way, not a follow-up event.
`BridgeCoordinator._resolve_reply_target` looks that id up via `find_group`
and, for each relay target, resolves it to *that target's own* counterpart id
before calling `ReceiverService.receive(..., reply_to_target_message_id=...)`
— silently `None` if the replied-to message was never relayed there (not
bridged at the time, sent before the bridge existed, or itself still
unsynced), the same silent-skip the other sync features use.

**Discord → Stoat only.** Discord → Stoat is native: discord.py exposes the
replied-to id at `Message.reference.message_id` (guarded on
`reference.type` so a forwarded message, not a real reply, doesn't carry one),
and Stoat's `channel.send(..., replies=[Reply(id, mention=False,
fail_if_not_exists=False)])` accepts it directly — passed only on the first
post of a split relay so a multi-chunk message doesn't reply N times,
`mention=False` so it doesn't re-ping the original author on every bridge
hop, `fail_if_not_exists=False` plus a same-spirit retry-without-it (mirroring
the color-retry) so a since-deleted counterpart doesn't sink the whole send.
**Stoat → Discord is not feasible**: a relay posts through the channel's
webhook (Execute Webhook), whose API has no `message_reference` field — a
webhook message can't be a native Discord reply, so `DiscordReceiverService`
leaves `supports_replies` unset and ignores the parameter. **IRC has no
reply/threading concept** — `supports_replies` stays `False` there too.

### Channel history backfill

`/mirror channel with history` (issue #122) optionally backfills a
freshly-linked channel with the source channel's message history, so linking
two channels doesn't start every reader at a blank slate. `ChannelLinker
.mirror_channel` requires only the *source* to support fetching history
(`ConnectorInfo.supports_history`, true whenever `fetch_history` is wired) —
the destination needs no such capability itself, since the backfill just
calls its ordinary `ReceiverService.receive()` (loosened from a symmetric
source-*and*-destination check in issue #141, which also wired IRC's own
`fetch_history` as a source). Each connector's `fetch_history(channel_id,
limit)` (`services/discord_service/sender.py` / `services/stoat_service
/sender.py` / `services/irc_service/sender.py`) returns `StandardMessage`s
oldest-first regardless of fetch direction — Discord's is a single
`channel.history()` call (reversed when `limit` is bounded, since bounded
means "the most recent N", fetched newest-first); Stoat's is hand-paginated
(`≤100`/page, stoat.py's own cap) since it has no single unbounded-history
call, walking backward (`sort=latest, before=cursor`) when bounded or forward
(`sort=oldest, after=cursor`) for the unbounded `all` case, reversing only
the bounded result; both drop the bridge's own messages, non-whitelisted
bots, and system-event rows the same way their live `_handle_message` does.

IRC has no on-demand history query at all — no CAP negotiation on this
network, and no per-channel scrollback API — only a chanhistory-style replay
(`_HISTORY_REPLAY_NOTICE_RE`, `_HISTORY_REPLAY_TIMEOUT`) that a server sends
right after JOIN, which the live-relay path (`_consume_history_replay`)
already detects and drops so it isn't relayed as if live. `IrcSenderService
.fetch_history` gets a *fresh* snapshot on demand by forcing a genuine PART
and immediate re-JOIN, then diverting that replay burst into a capture list
instead of dropping it (`HistoryReplayState.capture`/`.future`, tracked
per-channel in `_history_replay` for a replay already in progress and
`_pending_history_capture` for a fetch still waiting on its NOTICE to
arrive — never both at once for the same channel) — resolved once the
replay's own announced count is exhausted or `_HISTORY_REPLAY_TIMEOUT`
elapses, the latter also covering a channel with no chanhistory module at
all (no NOTICE ever arrives), which resolves to an empty list exactly like
Discord/Stoat's own "channel has no history" case. `limit` only narrows the
captured list client-side — there's no way to ask the server to replay more
than its own chanhistory cap already chose to send. Two concurrent
`fetch_history` calls for the same channel are serialized (`_history_fetch
_locks`) rather than racing each other's PART/re-JOIN cycle; `_history_replay`
/`_pending_history_capture` are touched from both the IRC reactor thread
(`_handle_pubnotice`/`_consume_history_replay`) and the asyncio loop thread
(`fetch_history` itself), guarded by a plain `threading.Lock`
(`_history_state_lock`) held only across the dict operations, never an
`await`. **Visibly disruptive**: the channel is genuinely parted and
rejoined while a fetch is in flight (typically well under
`_HISTORY_REPLAY_TIMEOUT`), during which anything sent there won't relay
live — an accepted, rare (once per `with_history` request) side effect,
unlike Discord/Stoat's silent API-based fetch.

A backfill *into* IRC additionally requires that connector's own
`default_channel_modes` to enable chanhistory (the `H` flag) —
`IrcSenderService.chanhistory_configured`, wired to `ConnectorInfo
.supports_history_destination` — checked by `mirror_channel` only when the
*destination* wires that hook (IRC is currently the only connector that
does; Discord/Stoat leave it unset, imposing no extra restriction) and
rejected up front ("History is not supported/configured on the target
service.") rather than silently landing a backfill that has no more
persistence than any other live IRC message there. A config-level check
(this connector's own setting, not a live per-channel MODE query), matching
this feature's existing "gold setup" scoping for chanhistory detection.
IRC's own `MIRROR CHANNEL` commands don't parse a `history:`/`with_history`
option themselves (see `COMMANDS.md`) — reach IRC as either side of a
backfill via Discord's or Stoat's `/mirror channel` naming it as the
`service`/`destination` instead.

`BridgeCoordinator.backfill_history` is the orchestration step `mirror_channel`
calls once a fresh link succeeds (never on the "already synced - skipped"
early return, so a repeat `with history` mirror on an already-linked pair is
a natural no-op rather than a duplicate backfill) — `ChannelLinker` itself
has no receiver reference, only `ConnectorInfo` hooks, so this is injected as
a `backfill_history` callback (bound to the coordinator in `bridge.py`'s
`run()`). It deliberately never uses `handle_incoming`'s fan-out: a history
replay must land on the *one new* destination only, not on every other
pre-existing bridge member of the source channel. Sequential (not
`asyncio.gather`), with a small pacing delay between sends on top of each
connector's own rate-limit handling. Best-effort per message — a
`PartialRelayError` or any other raising `receive()` counts as skipped;
`UnsupportedRelayTargetError` stops the whole backfill early, since a
structurally broken target fails identically on every remaining message.

`history_limit` (`admin_commands/channel.py`'s `_resolve_history_limit`)
defaults to 50 messages when omitted, accepts a positive integer clamped to
1000, or the literal `all` for the entire channel history with no cap — `all`
must be requested explicitly, never inferred from an unusually large number.
Backfilled messages are **not** recorded in `MessageSyncRepository`, so an
edit/reaction/pin on one of them won't sync forward, and a very long `all`
backfill relayed through a Discord slash command risks outliving the
interaction's 15-minute followup-token window — both known v1 limitations,
not oversights (tracked for a future pass rather than blocking this issue).
`with_history` also can't be combined with issue #123's entity-level
`local_id`/`source_id` `all` fan-out — `ChannelLinker.mirror_channel` rejects
the combination outright, since one backfill request has no sensible way to
apply across every enumerated channel at once.

`/import` / `/export` (issue #161; `IMPORT` / `EXPORT` on IRC) reuse the same
machinery to copy history between two channels that **already exist**, with
no create or link step: `ChannelLinker.transfer_history` picks source and
destination from the direction, resolves both channels (an unknown one is a
`LinkError`), rejects only an identical source and destination (a
same-connector transfer is fine), and runs the same gates as `with_history`
via the shared `_check_history_transfer`. It calls `backfill_history(...,
include_relayed=True)`, which passes `include_relayed=True` on to each
connector's `fetch_history` (only when set, so `/mirror ... with history` is
unchanged and still skips relayed and bot posts). In that mode a relayed post
is kept and read back under the identity it was shown with - Discord's
webhook name/avatar, Stoat's masquerade name/avatar/color, and IRC's
`<nick, Source, pronouns>` line tag parsed back by
`irc_service/formatting.parse_relayed_line` (paired with the receiver's
`format_line_tag`) - with `source_label`/`sender_pronouns` left `None` so the
destination doesn't decorate an already-decorated name twice. Bot posts are
kept as native. Nothing echoes across the destination's linked channels:
`backfill_history` never fans out, and every post it makes is the bridge's
own, which each sender's loop guard drops. `transfer_history` is
`@_guards_mirror`-wrapped reserving **both** connectors
(`_transfer_both_connectors`), so a transfer and any `/mirror` into either
one exclude each other.

### Source, pronoun & name-color forwarding

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

**Name color forwarding** (issue #74): every `StandardMessage` also carries
`sender_color` — the sender's displayed name color as a CSS color string,
resolved network-free by the origin sender from their top colored role
(`_member_color` in each service's `formatting.py`, gated by a per-connector
`color_forwarding`, default on, on `DiscordConnectorConfig` /
`StoatConnectorConfig`). Only **Stoat's receiver** consumes it — a
`MessageMasquerade` takes a `color`, so a relayed name is tinted with the
origin color; `color_forwarding` on the Stoat connector also gates that
inbound application. Discord (`Member.color`, resolved to `#rrggbb`; `0`
means no color → `None`) and Stoat (`Role.color`, an as-is CSS string, so
gradients forward too; lowest-`rank` colored role wins) both resolve
outbound; **IRC** has no user-color concept so `sender_color` is always
`None` there and it has no `color_forwarding` option. Discord webhooks and
IRC can't tint a relayed name, so those receivers ignore `sender_color`.
Setting a masquerade color needs the bridge bot's `manage_roles` permission
in the target channel — a Stoat send rejected with a color set is retried
once **uncolored** (for that chunk and the rest of a split message) rather
than lost.

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
`COMMANDS.md`, not duplicated here. Every connector also has a help command
with the same `[topic] [noun]` drill-down - Discord's `/help [topic]`
(a single dropdown of every subtopic), Stoat's `/bridge-help [topic] [noun]`
(kept under that name rather than `/help` to avoid colliding with other
bots' command providers in a shared server), and IRC's `HELP [topic]
[noun]` - all three rendered from one shared `admin_commands/help.py` table
(`HELP_TOPICS` + `render_help`/`resolve_help_key`) instead of each
maintaining its own hand-written text blob that drifted from `COMMANDS.md`
and from each other (issue #117). `/mirror <noun>` is a two-way group:
`to <service> …` pushes a local entity onto another connector (the historical
`/mirror <noun>` behavior), `from <service> <external_id>` pulls a remote
entity in and creates the local copy - respecting already-linked entities
(bridge/mapping groups are reused). Both directions require an explicit
`<service>` (issue #97) - `all` stays a valid value on `to` (fans out to
every other connector) but is no longer assumed when `service` is omitted;
the omitted-service default is gone from all three front ends' command
layers, not from the `admin_commands` linkers, which already required a
`destination`. `/mirror channel` in *either* direction
(and `all`) lands the counterpart in the destination's own copy of the source
channel's linked Category when that Category is `/link category`-linked -
resolved by `ChannelLinker._local_category_for_source_channel`, not by an
exact Category-name match - and only falls back to a same-named Category when
it isn't linked (issue #50). `/mirror channel` (both directions) also takes an
optional `category` - a Category id or name (`ChannelLinker.mirror_channel`'s
`destination_category`, resolved to a title by
`_resolve_destination_category_name`) that the counterpart is placed under,
overriding `category_from_channel_id` *and* the linked-Category lookup entirely
(issue #75): on `to` it's a Category on the destination service (so it needs a
single service, not `all`), on `from` a local Category (routed through the same
`destination_category`, since `mirror_channel_from` calls `mirror_channel` with
`destination` = the local connector). Discord models it as the native `category`
option; Stoat takes it as a `category:` key/value token
(`admin_commands.pop_kv_option`), IRC as a `-c`/`--category` flag
(`admin_commands.pop_flag_option`). Each connector has one convention for
named optional parameters (issue #167): Stoat uses `name:value` tokens named
after Discord's options (a bare `history` is a flag), IRC uses args-style
`-x VALUE` / `--long VALUE` flags. When `/mirror category` *creates* the
counterpart Category, it records it (and places the source Category's child
channels) under the title it just asked `ensure_category` for, not by
re-resolving the id through the connector's name cache - that cache is
populated at connect and blind to a brand-new Category, so re-resolving
handed back the raw id, which then got stored as the Category name and
passed on as a child-channel Category title, spawning a second Category
literally named after the id (issue #64). Every `/mirror` command
(`/mirror channel|category|role|emote`, all of `to`/`from`/`all`)
first force-refreshes both the source and destination connector's cached
server state via `ConnectorInfo.refresh` (`_refresh_connectors` in
`admin_commands/common.py`), so an entity created since the gateway connected isn't
missed by the cache-only `resolve_*`/`ensure_*`/`list_*` reads and then
duplicated (issue #81). Only Stoat wires `refresh`
(`StoatLookupsMixin.refresh` re-fetches the server + members + emoji and
writes them back into stoat.py's cache the way its own `ServerCreateEvent`
does, seeding the `_fresh_categories` cache off the same fetch) - stoat.py
1.2.1's cached `Server` genuinely drifts (patched from only a few gateway
events, Categories from none - issue #66); Discord's cache is kept live by
gateway events and IRC's channel state is already live, so both leave it
unset. Best-effort (a missing/raising hook is ignored) and throttled
(`services/caching.RefreshThrottle`, 10s) so a `... all` / `/mirror
category` fan-out that re-enters a per-destination mirror stays one network
round-trip. Both directions of every
`/mirror <noun>` take an optional `new_name` (`admin_commands/common.py`'s
`_clean_new_name`): the name the counterpart is created/matched under on the
destination instead of carrying the source name over - routed through the
destination's `ensure_*` hook so it's destination-normalized and still
get-or-creates (so a same-named existing entity is matched, not duplicated);
the way to aim `/mirror channel` at an unlinked existing destination channel,
IRC especially (issue #44). Not on the `all` fan-out; on Stoat it's a
`new_name:<name>` token, on IRC a `-n`/`--new-name` flag; `/mirror category`'s renames only the Category, not its
mirrored child channels. Any name the linker hands `ensure_channel` /
`ensure_category` / `ensure_role` - carried-over source name or `new_name`
override - is first clipped (`channel_structure.clip_name`) to the destination
`ConnectorInfo.channel_name_limit` / `category_name_limit` / `role_name_limit`
(Discord 100, Stoat 32, IRC's advertised `CHANNELLEN` or `RFC_CHANNEL_NAME_LIMIT`
= 50; `None` = no clip), so a name that fits the source platform isn't rejected
or silently mangled by the destination's API (issue #99). For channels the clip
runs *after* `_normalize_name` (IRC's `#`-prefix + sterilization) so the stored
name and the id `ensure_channel` returns can't disagree on length;
`IrcSenderService.ensure_channel` / `normalize_channel_name` also truncate to the
live `CHANNELLEN` as a backstop for paths other than `/mirror`. `/mirror channel` (both
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
a hand-typed id/name). Whatever the operator has already typed is *always*
offered back as its own `Use "<text>"` choice at the top of that menu
(unless it exactly matches a listed id), so a name/id the `list_*` hook
doesn't know about is still selectable rather than only enterable as blind
free text (issue #80); to keep that menu complete for members,
`DiscordSenderService._handle_ready` chunks the guild's full member roster
into cache on connect (privileged members intent) instead of relying on
whoever spoke while the bot was up. Shared logic
lives in the `admin_commands` package (`ChannelLinker` / `CategoryLinker` /
`EmoteLinker` / `UserLinker` / `RoleLinker`, one per module), called
identically from each connector's own `services/*.py` module. Nothing is
bridged (or mention-linked) automatically — every pair is linked explicitly
via those commands.

Every `mirror_*` entry point on those linkers is wrapped by
`_guards_mirror`, which reserves the operation's **destination
connector(s)** on a single shared `MirrorGuard` for the duration
(`bridge.run()` hands the one instance to all four linkers) — `… to` /
`… from` reserve the one destination, the `… all` fan-outs reserve *every*
other connector (`_mirror_all_other_connectors`). A `/mirror` — of any
entity kind — into a connector another `/mirror` is still writing to fails
fast with a user-facing `MirrorInProgressError` (a `LinkError` subclass, so
every "relay `str(exc)` to the admin" path already handles it), the message
naming which connector is busy, rather than racing it into duplicate
channels/Categories/roles/emoji (issue #79 — `/mirror channel` especially
is slow). An `… all` is all-or-nothing: one busy destination rejects the
whole fan-out before it starts rather than being silently skipped. The
reservation is keyed to the running asyncio task, so one operation that
fans out through several linker methods in the same task (`… all`,
`/mirror category` mirroring each child channel, `… from` delegating to
`… to`) re-enters freely, while a genuinely concurrent command — always a
separate task — is the one rejected. Mirrors into *different* destinations
still run in parallel. `CategoryLinker.sync_new_channel`'s auto-sync
catches the rejection and defers that channel (the manual mirror picks it
up if it's a child of the mirrored Category); `_handle_thread_create`'s
auto-mirror (via `mirror_channel_all`) catches it and logs a deferral —
the thread is re-tried on nothing, so it just isn't mirrored until the
operator re-runs.

When `/mirror channel` (or `/mirror channel from`, thread auto-mirror, or
linked-Category auto-sync) **creates** a counterpart channel, it carries the
source channel's cosmetic metadata over so the new channel isn't left blank
(`models.ChannelMetadata` — description, NSFW/maturity flag, icon URL,
slowmode delay in seconds). The source connector's `ConnectorInfo.describe_channel`
hook reads it, and it's passed to the destination's `ensure_channel` as a
`metadata=` keyword which each hook applies **only on the create path** — a
mirror that reuses/matches an existing channel leaves its metadata untouched.
Discord (topic + NSFW + slowmode, all native — `TextChannel.slowmode_delay` /
`create_text_channel(slowmode_delay=...)`; no per-channel icon) and Stoat
(description + NSFW + icon, the icon a best-effort `channel.edit` after
create; slowmode via a raw HTTP GET/PATCH on `/channels/{id}`'s `slowmode`
field — stoat.py 1.2.1's typed client doesn't model it, issue #108) both
implement `describe_channel` and channel creation; IRC's `ensure_channel`
sets only the channel TOPIC from `description` (and only when its JOIN just
created the channel — the server auto-ops the first joiner), leaves
`describe_channel` unset, and ignores NSFW/icon/slowmode. All best-effort —
a missing or raising `describe_channel` just means no metadata is carried.

`RoleLinker` (`storage/role_mappings.py`) is the role-level counterpart of
`ChannelLinker`. Every id argument also accepts a bare role name via each
connector's `resolve_role_id_by_name` hook; `/mirror role` creates-or-matches
a same-named role via the `ensure_role` hook (name only — color/permissions
aren't copied). Linked-role `<@&id>` / `<%id>` mentions are rewritten into the
target's linked role (`@Name` on IRC) alongside the user/channel mention
rewrites; an *unlinked* role mention is expanded to a plain `@Role Name`
(the role's name on the origin, carried on `StandardMessage.mentioned_roles` /
`StandardEdit.mentioned_roles` — populated best-effort by the Discord/Stoat
senders off the message's `role_mentions`, absent on IRC) rather than relayed
as the raw token (issue #4 — the role counterpart of the issue-#56 user fix),
and left exactly as it appeared only when even that name can't be recovered.
Like the user expansion it's run through `mentions._defang_mentions`.

A user `<@id>` mention is rewritten to the target's native mention of the
`/link-user`-linked identity where one exists; where it doesn't,
`rewrite_mentions` expands it to a plain `@Display Name` (the mentioned
user's name on the origin, carried on `StandardMessage.mentioned_users` —
populated best-effort by the Discord/Stoat senders off the message's
`mentions`, absent on IRC which has no structured mentions) rather than
relaying the raw `<@id>` token (issue #56). A mention the map can't name is
still left exactly as it appeared. A `<#id>` **channel** mention of a channel
that isn't `/link channel`-linked on the target is likewise expanded by
`rewrite_channel_mentions` to a plain `#channel-name` — the origin's name for
the channel, carried on `StandardMessage.mentioned_channels` /
`StandardEdit.mentioned_channels` (Discord off `Message.channel_mentions`,
Stoat by scanning the text and resolving each id via `get_channel_name`,
absent on IRC) — rather than relaying the raw `<#id>`, which renders as a dead
id on the target (issue #84); an unresolvable one is left as it appeared, and
the `#name` is run through `_defang_mentions` too. That expansion is the one place relayed
text picks up an `@`-prefixed token from an attacker-controlled string, so
it's run through `mentions._defang_mentions` (a zero-width space wedged in
after the sigil of any `@everyone` / `@here` / `<@…>` / `<#…>` / `<%…>` it
contains) — the bridge sets no `allowed_mentions` on its webhook/masquerade
sends, so an un-defanged `@everyone` in a display name would be a live mass
ping. It's also applied *after* the plain-word nick scan so an injected
name can't itself be re-read as a nick mention. Its keyword half,
`mentions.neutralize_mass_pings`, is also run by every receiver (ungated)
over the whole relayed text, on both the first relay and edit sync, so a
literal `@everyone` / `@here` the origin treated as inert (a forwarded
message, a user without Mention Everyone) can't ping on the target
(issue #163).

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
their channels alone. `local_id` also accepts the literal `all` (issue #160,
`_unlink_all_channels`): it runs the same kick/dissolve over every bridge
group the invoking connector has a stored mapping in. It needs an explicit
`service` (a connector, or `all` to dissolve every group), skips groups with
no member on that service, and reports failures per group rather than
aborting.

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
(see below) — those stay outside the bridge. A Discord **forum channel** is
linked as a Category too — `/link channel` / `/mirror channel` on a forum
redirect here (see "Discord forum channels as Categories" below).

A successful `/link <noun>`, a single-destination `/mirror <noun> to
<service>`, and a `/linked <noun>` on an already-linked target (invoker
gated on Manage Server) attach a `discord.ui` in-line editor panel to the
Discord reply — the project's first use of `discord.ui` components — so
retargeting an existing link doesn't mean retyping the whole command
(issue #115). `LinkEditorSpec`/`LinkEditorView`
(`services/discord_service/editor.py`) are the generic implementation: one
view class drives all five entity kinds through a small per-kind
`_KindAdapter` table (wrapping each linker's differently-shaped
`link_*`/`unlink_*`/`list_*` methods into a uniform shape) rather than five
near-duplicate views. `DiscordLinkingMixin._send_linker_reply` /
`_reply_linker_result` (`linking.py`) attach the view when a handler passes
an `editor=LinkEditorSpec(...)` built from the same arguments it just used
for the call; `_listing_editor` builds the `/linked <noun>` case's spec off
`describe_group`. `describe_group` (one per linker, alongside each linker's
existing `list_linked_*`) and `collect_linked_members`
(`admin_commands/common.py`, factored out of `format_linked_listing`) are
the structured (id/name, not pre-formatted string) seam the panel reads and
re-reads to rebuild itself. Retargeting is unlink-old-then-link-new, not a
single atomic operation — a rejected new link (already linked elsewhere,
unknown id, etc.) leaves the old edge gone too, same as running `/unlink`
then a failing `/link` by hand; the panel surfaces the `LinkError` and stays
open. **v1 scope is retarget + unlink only** — renaming the local entity or
moving a channel to a different Category aren't in the panel (deferred to a
follow-up); see `COMMANDS.md`'s "Editing a link in place" section for the
user-facing description.

### Bot whitelisting

A bot-authored message/edit/reaction is dropped by every sender by default —
the loop-guard side effect of relaying via Discord webhook / Stoat masquerade
means a bot-posted message looks the same as the bridge's own echo. Issue
#120's `/whitelist` / `/whitelisted` commands (Discord/Stoat only — IRC has
no bot concept and already relays every nick) manage a per-connector allowlist
of bot user ids whose activity relays like a human's, checked by
`BotWhitelistManager.is_whitelisted` (`admin_commands/bot_whitelist.py`),
cached ~60s per `(connector_id, user_id)` off the hot path
(`services/caching.AsyncTTLCache`, same pattern as pronoun resolution). Two
sources merge at check time: a static `config.yaml` `whitelisted_bots:` seed
(`"<source>:<id>"` pairs, parsed in `config.py`, never written to Mongo and
not removable by `/whitelist remove`) and `storage/bot_whitelist.py`'s
`BotWhitelistRepository` (runtime entries added/removed by the command).
**Respects `/link user` links**: if the whitelisted bot's identity is linked
across connectors, an event from *any* linked identity is treated as
whitelisted too (checked through `UserMappingRepository.get_link_group` /
`get_mapped_users`).

Each sender's loop guard stays unconditional and runs *before* the whitelist
check, so the linked-identity lookup can never re-admit the bridge's own
output even if the bridge bot ends up in a link group by mistake: Discord's
`message.webhook_id is not None` check (`_handle_message`) and
`data.get("webhook_id")` (`_handle_raw_message_edit`); Stoat's
`author_id == self._self_id` check in both `_handle_message` and
`_handle_message_update`. Only the *other* half of each gate — "is this
author a bot at all" — is relaxed by a whitelist hit. Reactions follow the
same shape: Discord's `_handle_raw_reaction` already excluded other bots
(`_is_other_bot`), now relaxed by the same check; Stoat's
`_handle_message_react` previously forwarded every other bot's reaction
unconditionally (an asymmetry with Discord) — it now excludes a non-
whitelisted bot's reaction too, resolving the reactor via the cache-only
`Client.get_user` (best-effort; a cache miss lets the reaction through, same
stance as Discord's removal-path fallback). Custom-emoji-*create* sync is
explicitly out of scope — a mirrored emoji's own creator is always the bridge
bot, so that check is a same-bot loop guard, not a bot-authorship filter.

`ConnectorInfo.self_user_id` (a lazy callable — the client may not be ready
when `bridge.py` wires it) is a guardrail against whitelisting the bridge's
own bot by id (`BotWhitelistManager.whitelist_bot` refuses it); the sender-
side checks above are the actual loop protection, not this one.

### Discord threads

Discord threads have no IRC/Stoat equivalent. `_handle_thread_create`
(`services/discord_service.py`) treats a Discord thread/forum-post as a new
Stoat/IRC channel rather than trying to map it onto their flat channel model:
it auto-mirrors (creates + links) the channel on every other connector via
`ChannelLinker.mirror_channel_all_for_thread`, placed under a Category named
after the thread's **parent channel** (so every thread under one parent
groups together on the destination) — using each destination's *own* linked
name for that parent channel (`category_from_channel_id`), not the Discord
name, and falling back to the Discord name only where the parent isn't linked
there. That Category's title is prefixed with a `🧵 #` thread marker
(`channel_structure.thread_category_title`, applied at the single
`ChannelLinker.mirror_channel`/`mirror_channel_for_thread` insertion point
every thread mirror funnels through, clipped to the same 32-char limit
afterward) so a bridge-generated thread group stands out from an ordinary
same-named Category — issue #98; the mirrored thread *channel* names are left
unprefixed (Stoat renders a client-side leading `#` on them already). It then
relays the thread's own starter message into it as the originating user, and
pins that relayed copy on every destination (`ReceiverService.set_pinned`,
via a synthetic `StandardPin` fed through `self._on_pin` /
`BridgeCoordinator.handle_pin`) — issue #124.

`mirror_channel_all_for_thread` (and its per-destination counterpart
`mirror_channel_for_thread`) exist specifically so that relay+pin happens
**before** the mirrored channel is placed into its thread Category, not
after: unlike `mirror_channel`/`mirror_channel_all` (still used by ordinary
`/mirror channel`, where ordering doesn't matter), they split each
destination's `ensure_channel` call in two — first with no `category` (just
create-or-match + link), then a second, deferred call (the returned `finish`
callback) that actually places the channel in its Category. `ensure_channel`
otherwise bundles create and categorize into one call; on Stoat that
categorize step is often a slow whole-server-PATCH (`_ensure_channel_in_category`),
so without this split the starter message would always land in an
already-organized channel instead of being the first thing posted there
(issue #124). On Stoat, if `group_parent_channel_with_threads` is set
(default on, per-connector), the parent channel itself is also moved into that
Category at the top — done once, when the deferred category-placement call
first creates the thread Category (issue #94 — the relay-path check below
reads the cache-only Category list, which never carries a Category
`ensure_channel` just made over raw HTTP until a reconnect/`refresh()`
repopulates it, so `/mirror channel` on a thread would otherwise finish
without grouping the parent), then re-checked on every relayed message
(`StoatSenderService.group_parent_channel_with_threads`, called from the
receiver) so enabling it mid-deployment takes effect without a restart. Discord's own "<user> started a thread" system message in
the parent channel is suppressed (`_handle_message`); `_handle_thread_create`
instead posts its own bot notice — `"<user> started a thread: <#thread>"` — but
only *after* the mirror+link finishes, so the `<#thread>` mention resolves. Each
receiver rewrites that mention (`services/mentions.py`'s
`rewrite_channel_mentions`, run alongside the user-mention rewrite) into its own
linked copy of the mirrored channel — `<#id>` on Discord/Stoat, `#channel` on
IRC — falling back to `#<thread name>` (carried on the notice's
`mentioned_channels`) if it still can't resolve. A thread with
no real starter message (standalone thread / forum-post system row) skips the
starter relay but keeps that row's author for the notice. Only
fires when the thread's parent channel is itself already bridged — a plain
channel via the channel mappings, or a forum parent via
`CategoryLinker.is_category_linked` (issue #100); one-way (Discord → Stoat/IRC). The destination Category is bound to the destination's
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
next thread for that parent; `group_parent_channel_with_threads`'s legacy
no-binding fallback (a direct Category-title↔channel-name match) strips the
`🧵 #` prefix off the title first (`strip_thread_category_prefix`) so an
old thread Category titled `🧵 #general` still matches parent channel
`general`.

Running `/mirror channel to <service>` (or `/mirror channel from discord <id>`
on the other connector) **on a Discord thread** now takes the same path: the
thread grouping isn't special-cased in the auto-mirror handler but in
`ChannelLinker.mirror_channel`, which calls the source connector's
`ConnectorInfo.resolve_thread_parent` hook (Discord-only —
`DiscordSenderService.get_thread_parent`) and, when the channel resolves to a
thread, flips `is_thread_category` on and points `category_from_channel_id` at
the parent channel itself — so a manually-mirrored thread lands under (and
binds) a Category named after its parent, and the parent channel gets pulled
into it (by `ensure_channel`, see the `group_parent_channel_with_threads`
note above — issue #94), instead of dropping into the parent's own linked
Category (issue #72).

A Discord **forum/media channel** itself (a `discord.ForumChannel`, not a
`Thread` under one) can't be a relay target — it has no top-level message
stream, so a webhook post into one needs a `thread_name`/`thread_id` the bridge
has no mapping for (Discord 400, error 220001). `DiscordReceiverService`'s
webhook resolver raises `UnsupportedRelayTargetError` for one (issue #69), which
`BridgeCoordinator` logs once (no traceback) and drops rather than retrying per
message. Relaying into a forum *post* (a `Thread` whose parent is the
ForumChannel) still works normally.

### Discord forum channels as Categories

A forum channel *acts* like a Category — its posts are threads, each already
mirrored as its own channel — so `/link channel` / `/mirror channel` on a
forum's own id **redirects into the Category flow** (issue #100):
`ChannelLinker.link_channel` / `mirror_channel` detect the forum via the
Discord-only `ConnectorInfo.is_forum_channel` hook and delegate to
`CategoryLinker.link_category` / `mirror_category`, so the forum links/creates
a **Stoat Category** up front rather than a flat channel. The mirrored Category
is titled `💬 #<forum-name>` (`channel_structure.forum_category_title`) to read
differently from an ordinary Category or a `🧵 #` thread group;
`strip_thread_category_prefix` strips either marker.
`CategoryLinker.mirror_category` enumerates a forum source's children via
`channels_in_category`, which for a `ForumChannel` returns its **active**
threads only (`forum.threads`) — archived posts are numerous and low-value and
still mirror lazily via `_handle_thread_create`.

Once the forum is Category-linked, `_handle_thread_create` fires for its posts
even though the forum isn't in the channel mappings — its gate also checks
`CategoryLinker.is_category_linked` against the forum id — and mirrors each
post as a channel into that linked `💬 #<forum>` Category (matched by title,
the same `mirror_channel_all` path ordinary threads take, forum-aware naming
applied at the one `is_thread_category` insertion point). A forum created
inside an already-linked Discord Category is auto-mirrored too
(`_handle_channel_create`'s `isinstance` filter includes `ForumChannel`) — but
because Stoat Categories don't nest, it becomes a **top-level** Stoat Category,
not a child of the mirrored parent Category.

**IRC keeps its current behavior**: it has no Category concept, so a
`/mirror channel` / `/mirror channel all` on a forum toward IRC falls through
to today's flat link, and each forum post becomes its own flat linked IRC
channel via the thread pipeline. The `UnsupportedRelayTargetError` guard in
`DiscordReceiverService` stays as the backstop for a leftover flat mapping
that still points a forum id at a relay target.

## Layout

```
config.yaml                    # every configured connector (Discord/Stoat/IRC) - gitignored, see config.yaml.example
Dockerfile / docker-compose.yml # bridge + auto-provisioned Mongo - see README's Docker section
tests/                          # pytest suite - see README's Tests section
src/stoat_discord_bridge/
  config.py                    # loads config.yaml, layering env vars over it per-field (see its docstring)
  models.py                    # StandardMessage - the platform-neutral message format
  channel_structure.py         # clip_name(name, limit=32) clips a mirrored channel/category/role name to a destination's limit (#99); thread_category_title adds the 🧵 # thread-group marker (#98), forum_category_title the 💬 # forum marker (#100)
  admin_commands/               # ChannelLinker / CategoryLinker / EmoteLinker / UserLinker / RoleLinker - shared linking logic
    common.py                   # ConnectorInfo hook dataclass, LinkError/MirrorInProgressError, MirrorGuard, pop_kv_option / pop_flag_option, id/name-resolution + conflict-check helpers
    channel.py / category.py / emote.py / user.py / role.py # one linker class per module - category.py depends on channel.py (mirrors a linked Category's child channels); the rest are independent
    help.py                     # HELP_TOPICS + render_help/resolve_help_key - shared help content for /help (Discord) / /bridge-help (Stoat) / HELP (IRC)
    __init__.py                 # re-exports every public name, so `from stoat_discord_bridge.admin_commands import <name>` still works unchanged
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
      # stoat_service/lookups/ and discord_service/lookups/ are themselves
      # packages (issue #92, the source-side counterpart of #90's
      # admin_commands split), each splitting the same connector concern by
      # area:
      #   stoat_service/lookups/: names.py (plain id<->name resolution) /
      #     identity.py (get_masquerade_identity) / channels.py (channel
      #     get-or-create) / categories.py (Category placement) / refresh.py
      #     (Category-list freshness + the #66/#81 cache-refresh machinery,
      #     split out of categories.py so neither module grows too large) /
      #     roles_emoji.py (role/emoji get-or-create) / listing.py (the
      #     autocomplete list_* hooks) - composed back into one
      #     StoatLookupsMixin by __init__.py.
      #   discord_service/lookups/: names.py / channels.py / categories.py /
      #     roles_emoji.py / listing.py, mirroring the Stoat breakdown above -
      #     but no identity.py or refresh.py counterpart, since Discord has no
      #     local-user-masquerade concept and its guild cache is kept live by
      #     gateway events rather than needing a freshness workaround -
      #     composed back into DiscordLookupsMixin by __init__.py.
      # Either way, every existing `from .../lookups import *LookupsMixin`
      # still works.
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
