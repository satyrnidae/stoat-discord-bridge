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
11. [ ] Spike: stoat.ext.commands usage/portability
  - see STOAT_BOT_POINTERS.md
