# TODOs

1.  [x] Feat: Split emote link command into /link emote subcommand structure
2.  [x] Feat: Add /unlink emote, /mirror emote, /linked emotes (IRC emote commands dropped - no custom emoji there)
3.  [ ] Feat: Remove /mirror-channels command (mass-mirroring dropped in favor of channel linking)
4.  [ ] Feat: IRC client username kwargs verification
5.  [ ] Feat: IRC receiver markdown stripping
6.  [ ] Integration: Verify Stoat role mentions against a live server
6a. [ ] Integration: Verify Stoat emoji hooks (Server.get_emoji / .emojis / fetch_emojis) behind /link emote, /mirror emote, /linked emotes against a live server
7.  [ ] Spike: Verify Stoat permissions flag names
8.  [ ] Spike: Verify various Stoat events attr member shapes:
  - stoat.events.ServerMemberUpdateEvent
  - stoat.events.RawServerRoleUpdateEvent
  - stoat.events.ChannelUpdateEvent
9.  [ ] Spike: Verify stoat.py's get_channel(partial=False) semantics
10. [ ] Spike: Verify stoat.py's Server.members structure
11. [x] Spike: stoat.ext.commands usage/portability
  - see STOAT_BOT_POINTERS.md
  - resolved: `_StoatClient` now subclasses `stoat.ext.commands.Bot`; admin
    commands are real `/link|unlink|linked|mirror` groups mirroring the Discord
    `app_commands` tree. `_is_admin` kept (not `has_server_permissions`) for
    graceful degradation; `manage_server` flag name confirmed vs the docs.
12. [ ] Refactor: Code maintainability: service files are too large; can be split into multiple classes by area of concern with similar structure mirrored by each service.
  - command parsing
  - mongo db
  - formatting
  - setup / teardown
13. [x] Fix: Ensure stoat commands check against actual permissions as defined on https://stoatpy.readthedocs.io/en/latest/api/enums_and_flag_classes.html#permissions
  - command execution gating (manage_server)
  - role sync (discord perm <-> stoat perm)
  - for discord perms, see https://discordpy.readthedocs.io/en/latest/api.html?highlight=permission#discord.Permissions
  - resolved: flag names on both sides verified against the stoat.py /
    discord.py `Permissions` classes (all were already correct);
    `NEUTRAL_PERMISSIONS` widened with embed_links/attach_files/add_reactions;
    `_is_admin` gained a server-owner fallback for permission-cache misses.
14. [x] Feat: Configurable Stoat command prefix char ('/' default, can be '!' etc.)
  - `StoatConnectorConfig.command_prefix` (env `STOAT__<i>__COMMAND_PREFIX`);
    threaded into `_StoatClient`'s `commands.Bot` ctor and the `/bridge-help`
    / usage strings (`_help_text`, `_StoatClient._prefix`).
15. [ ] Feat: x is typing... forwarding
