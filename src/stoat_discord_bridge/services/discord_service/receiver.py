"""`DiscordReceiverService`: posts `StandardMessage`s into Discord via webhook.

Posts "as" the originating Stoat/IRC user through a per-channel Discord
webhook (username + avatar override) rather than the bridge bot's own
identity. `target_channel_id` is a real Discord channel id; the receiver
resolves/creates that channel's webhook itself (cached per channel), so
`/link channel` never needs an admin to already have a webhook URL in hand.
"""

from __future__ import annotations

import asyncio
import logging
import time
from io import BytesIO

import aiohttp
import discord

# Imported as a module (not `from … import _CONTENT_LIMIT`) so a test patching
# `stoat_discord_bridge.services.discord_service._CONTENT_LIMIT` is picked up
# by `receive()` at call time - the historical monkeypatch seam.
import stoat_discord_bridge.services.discord_service as _discord_pkg
from stoat_discord_bridge.models import CustomEmoji, StandardMessage
from stoat_discord_bridge.services.base import PartialRelayError, ReceiverService
from stoat_discord_bridge.services.discord_service.formatting import (
    _USERNAME_LIMIT,
    _discord_reaction_matches,
    _sanitize_emoji_name,
    _sanitize_username,
    _to_discord_emoji,
)
from stoat_discord_bridge.services.formatting import (
    chunk_content,
    decorate_sender_name,
    download_attachments,
    inline_attachment_urls,
)
from stoat_discord_bridge.services.mentions import (
    rewrite_channel_mentions,
    rewrite_emoji,
    rewrite_mentions,
    rewrite_role_mentions,
)
from stoat_discord_bridge.storage.channel_mappings import ChannelMappingRepository
from stoat_discord_bridge.storage.emoji_mappings import EmojiMappingRepository
from stoat_discord_bridge.storage.role_mappings import RoleMappingRepository
from stoat_discord_bridge.storage.user_mappings import UserMappingRepository

logger = logging.getLogger(__name__)


class DiscordReceiverService(ReceiverService):
    supports_reactions = True
    supports_emoji = True
    supports_pins = True
    supports_typing = True

    def __init__(
        self,
        client: discord.Client,
        guild_id: int,
        connector_id: str,
        user_mappings: UserMappingRepository | None = None,
        enable_local_user_masquerade: bool = True,
        channel_mappings: ChannelMappingRepository | None = None,
        role_mappings: RoleMappingRepository | None = None,
        emoji_mappings: EmojiMappingRepository | None = None,
        source_forwarding: bool = True,
        pronoun_forwarding: bool = True,
    ) -> None:
        self._client = client
        self._guild_id = guild_id
        self.connector_id = connector_id
        self._user_mappings = user_mappings
        self._channel_mappings = channel_mappings
        self._role_mappings = role_mappings
        self._emoji_mappings = emoji_mappings
        self._enable_local_user_masquerade = enable_local_user_masquerade
        self._source_forwarding = source_forwarding
        self._pronoun_forwarding = pronoun_forwarding
        self._session: aiohttp.ClientSession | None = None
        self._webhooks: dict[str, discord.Webhook] = {}
        # target_channel_id -> monotonic deadline the keep-alive loop stops at,
        # and the loop task itself (one per channel currently "typing").
        self._typing_until: dict[str, float] = {}
        self._typing_tasks: dict[str, asyncio.Task] = {}

    async def receive(self, message: StandardMessage, *, target_channel_id: str) -> list[str]:
        webhook, thread = await self._get_or_create_webhook(target_channel_id)
        sender_name = message.sender_name
        avatar_url = message.sender_avatar_url
        if self._user_mappings is not None and self._enable_local_user_masquerade:
            local_identity = await self._resolve_local_identity(message)
            if local_identity is not None:
                sender_name, avatar_url = local_identity
        elif self._user_mappings is not None:
            logger.debug(
                "[discord:%s] local user masquerade disabled (enable_local_user_masquerade=false), "
                "not resolving local identity for sender %s",
                self.connector_id,
                message.sender_user_id,
            )
        # Cap to the webhook-username limit here (dropping the "[Source,
        # pronouns]" suffix whole rather than mid-token) so _sanitize_username's
        # own hard slice never bisects the bracket.
        sender_name = decorate_sender_name(
            sender_name,
            source=message.source_label if self._source_forwarding else None,
            pronouns=message.sender_pronouns if self._pronoun_forwarding else None,
            max_len=_USERNAME_LIMIT,
        )
        username = _sanitize_username(sender_name)
        # Re-upload the message's attachments as native Discord files rather
        # than pasting their (often short-lived, signed) CDN URLs into the
        # text - see issue #39. Anything too large or unfetchable falls back
        # to an inlined URL below so it's not lost.
        files, undownloadable = await download_attachments(message.attachments)
        content = message.content_markdown or ""
        if self._user_mappings is not None:
            content = await rewrite_mentions(
                content,
                origin_connector_id=message.origin_connector_id,
                target_connector_id=self.connector_id,
                target_kind="discord",
                user_mappings=self._user_mappings,
                mentioned_users=message.mentioned_users,
            )
        if self._channel_mappings is not None:
            content = await rewrite_channel_mentions(
                content,
                origin_connector_id=message.origin_connector_id,
                target_connector_id=self.connector_id,
                target_kind="discord",
                channel_mappings=self._channel_mappings,
            )
        if self._role_mappings is not None:
            content = await rewrite_role_mentions(
                content,
                origin_connector_id=message.origin_connector_id,
                target_connector_id=self.connector_id,
                target_kind="discord",
                role_mappings=self._role_mappings,
            )
        if self._emoji_mappings is not None:
            content = await rewrite_emoji(
                content,
                origin_connector_id=message.origin_connector_id,
                target_connector_id=self.connector_id,
                target_kind="discord",
                emoji_mappings=self._emoji_mappings,
            )
        if undownloadable:
            content = inline_attachment_urls(content, undownloadable)
        if content:
            chunks = chunk_content(content, _discord_pkg._CONTENT_LIMIT)
        else:
            # A file-only message needs an empty content, not the zero-width
            # sentinel; keep the sentinel only when there's nothing to send.
            chunks = [""] if files else ["​"]
        discord_files = [discord.File(BytesIO(data), filename=name) for name, data in files]
        ids: list[str] = []
        for index, chunk in enumerate(chunks):
            attach = discord_files if discord_files and index == len(chunks) - 1 else None
            logger.debug(
                "[discord:%s] sending webhook message to channel %s as %r (avatar_url=%r, files=%d): %r",
                self.connector_id,
                target_channel_id,
                username,
                avatar_url,
                len(attach) if attach else 0,
                chunk,
            )
            try:
                sent = await webhook.send(
                    content=chunk,
                    username=username,
                    avatar_url=avatar_url,
                    wait=True,
                    **({"thread": thread} if thread is not None else {}),
                    **({"files": attach} if attach else {}),
                )
            except Exception as exc:
                raise PartialRelayError(ids, exc) from exc
            logger.debug("[discord:%s] webhook message sent, id=%s", self.connector_id, sent.id)
            ids.append(str(sent.id))
        return ids

    async def add_reaction(self, *, target_channel_id: str, target_message_id: str, emoji: str | CustomEmoji) -> None:
        """Idempotent: skips the API call if the bridge bot has already
        reacted with this emoji (a second origin user reacting with the same
        emoji must not double-add)."""
        if await self._bot_has_reaction(target_channel_id, target_message_id, emoji) is True:
            return
        message = await self._get_partial_message(target_channel_id, target_message_id)
        await message.add_reaction(self._discord_emoji(emoji))

    async def remove_reaction(
        self, *, target_channel_id: str, target_message_id: str, emoji: str | CustomEmoji
    ) -> None:
        """Idempotent: skips the API call if the bridge bot isn't currently
        reacting with this emoji."""
        if await self._bot_has_reaction(target_channel_id, target_message_id, emoji) is False:
            return
        message = await self._get_partial_message(target_channel_id, target_message_id)
        await message.remove_reaction(self._discord_emoji(emoji), self._client.user)

    async def set_pinned(self, *, target_channel_id: str, target_message_id: str, pinned: bool) -> None:
        channel = self._client.get_channel(int(target_channel_id)) or await self._client.fetch_channel(
            int(target_channel_id)
        )
        try:
            message = await channel.fetch_message(int(target_message_id))
        except discord.HTTPException:
            return  # message gone, or we can't see it - best-effort
        if message.pinned == pinned:
            return  # already in the desired state - avoids a needless API call and echo
        try:
            if pinned:
                await message.pin(reason="bridge pin sync")
            else:
                await message.unpin(reason="bridge pin sync")
        except discord.HTTPException:
            logger.warning(
                "[discord:%s] couldn't %s message %s in channel %s",
                self.connector_id,
                "pin" if pinned else "unpin",
                target_message_id,
                target_channel_id,
            )

    # Discord shows a typing indicator for ~10s per call and has no API to
    # clear one early, so the best we can do on an explicit stop is quit
    # re-arming it. While the origin user keeps typing we refresh well inside
    # the ~10s window so the indicator never visibly flickers; `_TYPING_LINGER`
    # is the grace period we keep refreshing for after the last event, in case
    # a `stop_typing` never arrives (e.g. the origin is another Discord, which
    # emits no stop event).
    _TYPING_REFRESH = 2.0
    _TYPING_LINGER = 3.0

    async def trigger_typing(self, *, target_channel_id: str) -> None:
        """Show a typing indicator in the channel, attributed to the bridge bot
        (Discord can't show a webhook identity as typing). Extends the linger
        deadline and, if no keep-alive loop is running for this channel, starts
        one that re-sends the indicator every `_TYPING_REFRESH`s until the
        deadline (or an explicit `stop_typing`). Best-effort: a missing channel
        or transient API error just stops the loop."""
        self._typing_until[target_channel_id] = time.monotonic() + self._TYPING_LINGER
        task = self._typing_tasks.get(target_channel_id)
        if task is not None and not task.done():
            return
        self._typing_tasks[target_channel_id] = asyncio.ensure_future(
            self._keep_typing(target_channel_id)
        )

    async def stop_typing(self, *, target_channel_id: str) -> None:
        """The origin user stopped typing before sending. Discord has no
        clear-typing API, so all we can do is stop re-arming the indicator and
        let Discord's own ~10s timeout lapse it."""
        self._typing_until.pop(target_channel_id, None)
        task = self._typing_tasks.pop(target_channel_id, None)
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except BaseException:
                pass

    async def _keep_typing(self, channel_id: str) -> None:
        try:
            channel = self._client.get_channel(int(channel_id)) or await self._client.fetch_channel(
                int(channel_id)
            )
            while time.monotonic() < self._typing_until.get(channel_id, 0.0):
                await channel.typing()
                await asyncio.sleep(self._TYPING_REFRESH)
        except asyncio.CancelledError:
            raise
        except (discord.HTTPException, ValueError):
            logger.debug("[discord:%s] typing keep-alive for channel %s failed", self.connector_id, channel_id)
        finally:
            self._typing_until.pop(channel_id, None)
            self._typing_tasks.pop(channel_id, None)

    def _discord_emoji(self, emoji: str | CustomEmoji) -> str | discord.Emoji | discord.PartialEmoji:
        """Like the module-level `_to_discord_emoji`, but resolves a custom
        emoji against the client cache first: the real `discord.Emoji` carries
        the authoritative `name`/`animated`, and Discord's reaction endpoint
        rejects a `name:id` pair whose name is blank or whose animated prefix
        is wrong with "Unknown Emoji"."""
        if isinstance(emoji, CustomEmoji):
            getter = getattr(self._client, "get_emoji", None)
            resolved = getter(int(emoji.native_id)) if getter is not None else None
            if resolved is not None:
                return resolved
        return _to_discord_emoji(emoji)

    async def _bot_has_reaction(
        self, channel_id: str, message_id: str, emoji: str | CustomEmoji
    ) -> bool | None:
        """True/False if the bot's own reaction with `emoji` on that message
        could be determined from a fresh fetch, None if it couldn't - callers
        treat None as "act anyway"."""
        try:
            channel = self._client.get_channel(int(channel_id)) or await self._client.fetch_channel(int(channel_id))
            message = await channel.fetch_message(int(message_id))
        except Exception:
            return None
        want = self._discord_emoji(emoji)
        for reaction in message.reactions:
            if _discord_reaction_matches(reaction.emoji, want):
                return bool(reaction.me)
        return False

    async def create_emoji(self, emoji: CustomEmoji) -> CustomEmoji | None:
        guild = self._client.get_guild(self._guild_id)
        if guild is None:
            return None
        if self._session is None:
            self._session = aiohttp.ClientSession()
        try:
            async with self._session.get(emoji.image_url) as resp:
                resp.raise_for_status()
                image_bytes = await resp.read()
            created = await guild.create_custom_emoji(name=_sanitize_emoji_name(emoji.name), image=image_bytes)
        except (discord.HTTPException, aiohttp.ClientError) as exc:
            logger.warning(
                "[discord:%s] couldn't create emoji %r in guild %s: %s", self.connector_id, emoji.name, guild.id, exc
            )
            return None  # emoji slots full, name taken, image too large, etc. - skip this platform
        return CustomEmoji(
            native_id=str(created.id), name=created.name, image_url=str(created.url), animated=created.animated
        )

    async def _resolve_local_identity(self, message: StandardMessage) -> tuple[str, str | None] | None:
        """If `message`'s sender is linked (via /link-user) to a Discord
        identity on this connector, return that identity's (display_name,
        avatar_url) to masquerade as instead of the remote sender's own -
        None if unlinked or the linked id can't be resolved at all. Prefers
        the guild Member (whose server nickname/avatar override is what
        `_to_standard_message` uses for a *native* Discord message's own
        sender_name/avatar) over the global User, which only has an
        account-wide username/avatar - using the User here would show a
        linked user's username instead of their nickname in this guild."""
        local_user_id = await self._user_mappings.find_linked_user_id(
            message.origin_connector_id, message.sender_user_id, self.connector_id
        )
        if local_user_id is None:
            return None
        identity = await self._fetch_member(local_user_id) or await self._fetch_user(local_user_id)
        if identity is None:
            logger.warning(
                "[discord:%s] local user masquerade failed: linked user %s couldn't be resolved to a "
                "guild member or a global user",
                self.connector_id,
                local_user_id,
            )
            return None
        name = getattr(identity, "display_name", None)
        if not name:
            logger.warning(
                "[discord:%s] local user masquerade failed: linked user %s resolved but has no usable display name",
                self.connector_id,
                local_user_id,
            )
            return None
        avatar = getattr(identity, "display_avatar", None)
        avatar_url = str(avatar.url) if avatar else None
        logger.debug(
            "[discord:%s] resolved local user masquerade identity for %s: name=%r avatar_url=%r",
            self.connector_id,
            local_user_id,
            name,
            avatar_url,
        )
        return name, avatar_url

    async def _fetch_member(self, user_id: str) -> discord.Member | None:
        guild = self._client.get_guild(self._guild_id)
        if guild is None:
            return None
        try:
            return guild.get_member(int(user_id)) or await guild.fetch_member(int(user_id))
        except (discord.HTTPException, discord.NotFound, ValueError):
            return None

    async def _fetch_user(self, user_id: str) -> discord.User | None:
        try:
            return self._client.get_user(int(user_id)) or await self._client.fetch_user(int(user_id))
        except (discord.HTTPException, discord.NotFound, ValueError):
            return None

    async def _get_or_create_webhook(self, channel_id: str) -> tuple[discord.Webhook, discord.Thread | None]:
        # Threads have no webhooks of their own - a webhook belongs to (and
        # is fetched/created on) the thread's parent channel, and posting
        # into the thread itself is done by passing thread= to
        # Webhook.send() below. Cache under the parent's id so every thread
        # under it shares one webhook instead of creating a new one each.
        channel = self._client.get_channel(int(channel_id)) or await self._client.fetch_channel(int(channel_id))
        thread = channel if isinstance(channel, discord.Thread) else None
        webhook_channel = channel.parent if thread is not None else channel
        cache_key = str(webhook_channel.id)
        webhook = self._webhooks.get(cache_key)
        if webhook is not None:
            return webhook, thread
        existing = next((w for w in await webhook_channel.webhooks() if w.user == self._client.user), None)
        if existing is not None:
            webhook = existing
        else:
            # A per-message avatar_url override (the relayed sender's own
            # avatar) is passed to webhook.send() below when available, but
            # when it's not (e.g. sender_avatar_url couldn't be resolved),
            # Discord falls back to the webhook's own avatar - give it the
            # bot's, rather than Discord's blank/generic default, so an
            # unattributable message still looks like it came from *this*
            # bridge rather than nothing at all.
            avatar_bytes = await self._client.user.display_avatar.read()
            webhook = await webhook_channel.create_webhook(name="Bridge", avatar=avatar_bytes)
            logger.info("[discord:%s] created bridge webhook in channel %s", self.connector_id, cache_key)
        self._webhooks[cache_key] = webhook
        return webhook, thread

    async def _get_partial_message(self, target_channel_id: str, target_message_id: str) -> discord.PartialMessage:
        channel = self._client.get_channel(int(target_channel_id)) or await self._client.fetch_channel(
            int(target_channel_id)
        )
        return channel.get_partial_message(int(target_message_id))

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None
