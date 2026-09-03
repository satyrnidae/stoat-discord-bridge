"""Admin DM command parsing and dispatch for the IRC connector.

Admin commands arrive as a DM to the bot's own nick, bare and uppercase
(no leading "/" or "!" - unlike Discord/Stoat's slash commands, since many
IRC clients swallow a leading "/" as a local client command). The channel
and user commands are two-token (`LINK CHANNEL` / `MIRROR CHANNEL` /
`UNLINK CHANNEL` / `LINK USER` / `UNLINK USER`, and read-only `LINKED
CHANNELS` / `LINKED USERS`), matching Discord's `/link channel` subcommand
shape. IRC has no custom-emoji concept, so the emote commands aren't
offered here at all (same as roles/categories). See
`IrcSenderService._handle_privmsg` / `IrcAdminCommandsMixin._handle_dm_command`.
"""

from __future__ import annotations

import asyncio
import logging

from stoat_discord_bridge.admin_commands import LinkError

logger = logging.getLogger(__name__)

_ADMIN_DM_CHANNEL_VERBS = frozenset({"LINK", "MIRROR", "UNLINK"})
# Second tokens accepted after a verb in _ADMIN_DM_CHANNEL_VERBS.
_ADMIN_DM_TWO_WORD_NOUNS = frozenset({"CHANNEL", "USER"})

# IRC has no slash-command discoverability at all, hence HELP. See
# COMMANDS.md for full per-command detail - this is a compact pointer to it.
_HELP_TEXT = """Commands (DM me, bare and uppercase - see COMMANDS.md for full detail):
  STATUS - sync target health, read-only
  LINKED CHANNELS <local_id> - channels bridged to <local_id>, read-only
  LINKED USERS [local_id|name] - cross-connector user links, read-only
  LINK CHANNEL <local_id> <service> <external_id> - bridge a channel (IRC-operator)
  LINK USER <service> <external_id|name> <local_id|name> - link a user for mentions/masquerading (IRC-operator)
  MIRROR CHANNEL TO [service|all] <local_id> [AS <new_name>] - create+link a matching channel elsewhere (IRC-operator)
  MIRROR CHANNEL FROM <service> <external_id> [AS <new_name>] - create+link a local channel mirroring a remote one (IRC-operator)
  UNLINK CHANNEL <local_id> [service|all] - unlink a channel from one connector, or the whole group (IRC-operator)
  UNLINK USER [service|all] [local_id|name] - unlink a user (default: yourself) from one connector, or the whole group (IRC-operator)
  HELP - this message"""


class IrcAdminCommandsMixin:
    """Command parsing/dispatch half of `IrcSenderService`. Relies on the
    service for `connector_id`, `_linker`, `_user_linker`, `_loop`,
    `_pending_whois`, `connection`, and `_schedule`."""

    async def _handle_linked_channels_command(self, nick: str, args: list[str]) -> None:
        # Unlike Discord/Stoat, an IRC DM has no "current channel" context to
        # default to (same reasoning as MIRROR CHANNEL's local_id),
        # so the channel to look up is always required here.
        if len(args) != 1:
            self._notify(nick, "Usage: LINKED CHANNELS <local_id>")
            return
        if self._linker is None:
            self._notify(nick, "Linking isn't configured.")
            return
        summary = await self._linker.list_linked_channels(local_connector=self.connector_id, local_channel_id=args[0])
        self._notify(nick, summary)

    async def _handle_linked_users_command(self, nick: str, args: list[str]) -> None:
        """`LINKED USERS [local_id]`: with no argument, lists every
        cross-connector user link (for debugging); given an IRC nick, shows
        just that identity's link."""
        if self._user_linker is None:
            self._notify(nick, "Linking isn't configured.")
            return
        if args:
            summary = await self._user_linker.list_linked_users(local_connector=self.connector_id, local_user_id=args[0])
        else:
            summary = await self._user_linker.list_linked_users()
        self._notify(nick, summary)

    async def _handle_dm_command(self, nick: str, content: str) -> None:
        parts = content.split()
        if len(parts) > 1 and parts[0].upper() in _ADMIN_DM_CHANNEL_VERBS and parts[1].upper() in _ADMIN_DM_TWO_WORD_NOUNS:
            noun = parts[1].upper()
            # Channel commands keep the two-word `LINK CHANNEL` form the
            # branch bodies match on; user commands fold to `LINK_USER` /
            # `UNLINK_USER` to match theirs.
            sep = " " if noun == "CHANNEL" else "_"
            command, args = f"{parts[0].upper()}{sep}{noun}", parts[2:]
        else:
            command, *args = parts
            command = command.upper()
        if not await self._check_is_oper(nick):
            logger.info("[irc:%s] %s tried admin command %s without oper status - denied", self.connector_id, nick, command)
            self._notify(nick, "You need to be an IRC operator to do that.")
            return
        logger.info("[irc:%s] %s ran %s %s", self.connector_id, nick, command, " ".join(args))
        if command == "LINK CHANNEL":
            if len(args) != 3:
                self._notify(nick, "Usage: LINK CHANNEL <local_id> <service> <external_id>")
                return
            local_id, service, external_id = args
            if self._linker is None:
                self._notify(nick, "Linking isn't configured.")
                return
            try:
                summary = await self._linker.link_channel(
                    local_connector=self.connector_id,
                    local_channel_id=local_id,
                    local_channel_name=local_id,
                    source=service,
                    source_id=external_id,
                    destination_id=local_id,
                )
            except LinkError as exc:
                logger.info("[irc:%s] %s rejected: %s", self.connector_id, command, exc)
                self._notify(nick, str(exc))
                return
            self._notify(nick, summary)
        elif command == "LINK_USER":
            if len(args) != 3:
                self._notify(nick, "Usage: LINK USER <service> <external_id|name> <local_id|name>")
                return
            service, external_id, local_id = args
            if self._user_linker is None:
                self._notify(nick, "User linking isn't configured.")
                return
            try:
                summary = await self._user_linker.link_user(
                    local_connector=self.connector_id,
                    local_user_id=local_id,
                    source=service,
                    source_user_id=external_id,
                )
            except LinkError as exc:
                logger.info("[irc:%s] %s rejected: %s", self.connector_id, command, exc)
                self._notify(nick, str(exc))
                return
            self._notify(nick, summary)
        elif command == "MIRROR CHANNEL":
            # `MIRROR CHANNEL TO [service|all] <local_id>` pushes a local
            # channel onto another connector; `MIRROR CHANNEL FROM <service>
            # <external_id>` pulls a remote channel in and creates the local
            # copy. Both lead with `<service>` (matching Discord/Stoat); the
            # local id is always required on `TO` (an IRC DM has no "current
            # channel"), so 1 arg is just the id (service defaults to `all`),
            # 2 args are service then id.
            direction = args[0].upper() if args else ""
            rest = args[1:]
            # An optional trailing `AS <new_name>` renames the counterpart on
            # the destination instead of carrying the source name over (issue
            # #44) - split it off before the positional parse below. Only the
            # single-destination TO and the FROM forms honour it (a fan-out
            # `all` has many destinations, so one name can't apply).
            new_name: str | None = None
            if len(rest) >= 2 and rest[-2].upper() == "AS":
                new_name = rest[-1]
                rest = rest[:-2]
            if self._linker is None:
                self._notify(nick, "Linking isn't configured.")
                return
            try:
                if direction == "TO" and len(rest) == 1:
                    summary = await self._linker.mirror_channel_all(
                        local_connector=self.connector_id, local_channel_id=rest[0], local_channel_name=rest[0]
                    )
                elif direction == "TO" and len(rest) == 2 and rest[0].lower() == "all":
                    summary = await self._linker.mirror_channel_all(
                        local_connector=self.connector_id, local_channel_id=rest[1], local_channel_name=rest[1]
                    )
                elif direction == "TO" and len(rest) == 2:
                    summary = await self._linker.mirror_channel(
                        local_connector=self.connector_id,
                        local_channel_id=rest[1],
                        local_channel_name=rest[1],
                        destination=rest[0],
                        new_name=new_name,
                    )
                elif direction == "FROM" and len(rest) == 2:
                    summary = await self._linker.mirror_channel_from(
                        local_connector=self.connector_id, source=rest[0], source_id=rest[1], new_name=new_name
                    )
                else:
                    self._notify(
                        nick,
                        "Usage: MIRROR CHANNEL TO [service|all] <local_id> [AS <new_name>] | "
                        "MIRROR CHANNEL FROM <service> <external_id> [AS <new_name>]",
                    )
                    return
            except LinkError as exc:
                logger.info("[irc:%s] %s rejected: %s", self.connector_id, command, exc)
                self._notify(nick, str(exc))
                return
            self._notify(nick, summary)
        elif command == "UNLINK CHANNEL":
            # Same "no current channel" reasoning as MIRROR CHANNEL -
            # local_id is always required, hoisted to the first arg.
            # service is optional and defaults to "all" (dissolving the
            # whole bridge group), so 1 arg is just the channel and 2 args
            # are channel then service.
            if len(args) == 1:
                local_id, service = args[0], None
            elif len(args) == 2:
                local_id, service = args
            else:
                self._notify(nick, "Usage: UNLINK CHANNEL <local_id> [service|all]")
                return
            if self._linker is None:
                self._notify(nick, "Linking isn't configured.")
                return
            try:
                summary = await self._linker.unlink_channel(
                    local_connector=self.connector_id, local_channel_id=local_id, destination=service
                )
            except LinkError as exc:
                logger.info("[irc:%s] %s rejected: %s", self.connector_id, command, exc)
                self._notify(nick, str(exc))
                return
            self._notify(nick, summary)
        elif command == "UNLINK_USER":
            # Unlike UNLINK CHANNEL, both args are optional here: service
            # defaults to "all", and local_id defaults to the nick
            # running the command - IRC has no "current channel" to fall
            # back to, but it does always know who's asking.
            if len(args) > 2:
                self._notify(nick, "Usage: UNLINK USER [service|all] [local_id|name]")
                return
            service = args[0] if args else None
            local_id = args[1] if len(args) > 1 else nick
            if self._user_linker is None:
                self._notify(nick, "User linking isn't configured.")
                return
            try:
                summary = await self._user_linker.unlink_user(
                    local_connector=self.connector_id, local_user_id=local_id, destination=service
                )
            except LinkError as exc:
                logger.info("[irc:%s] %s rejected: %s", self.connector_id, command, exc)
                self._notify(nick, str(exc))
                return
            self._notify(nick, summary)

    async def _check_is_oper(self, nick: str) -> bool:
        # WHOIS is async on this library (reply numerics arrive later, on
        # the reactor thread) - issue the query and await its resolution via
        # a Future that _resolve_whois fulfils from that thread. Always live
        # (never cached), since oper status can be granted/revoked at any
        # time on the network.
        key = nick.lower()
        future: asyncio.Future = self._loop.create_future()
        self._pending_whois[key] = future
        self.connection.whois([nick])
        try:
            return await asyncio.wait_for(future, timeout=10)
        except asyncio.TimeoutError:
            logger.warning("[irc:%s] WHOIS for %s timed out - treating as not-oper", self.connector_id, nick)
            return False
        finally:
            self._pending_whois.pop(key, None)

    def _resolve_whois(self, nick: str, is_oper: bool) -> None:
        # Runs on the IRC reactor's own thread (see _IrcClient's on_whois*
        # callbacks) - the Future it resolves is awaited by a coroutine on
        # the asyncio loop, so it must be resolved via call_soon_threadsafe,
        # never by calling set_result directly from this thread.
        future = self._pending_whois.get(nick.lower())
        if future is None or future.done():
            # Either no _check_is_oper call is waiting on this nick, or
            # on_whoisoperator already resolved it True and this is the
            # trailing on_endofwhois/on_nosuchnick for the same exchange.
            return
        self._loop.call_soon_threadsafe(future.set_result, is_oper)

    def _notify(self, nick: str, text: str) -> None:
        for line in text.splitlines():
            self.connection.notice(nick, line)
