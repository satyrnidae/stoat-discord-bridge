"""`StoatReceiverService`: posts `StandardMessage`s into Stoat via masquerade.

Masquerade is a `send()` kwarg (`MessageMasquerade(name=, avatar=)`), not a
separate webhook-style API, so this reuses the already-connected sender
client for the same server rather than needing its own identity to post
through. The bot must have the `use_masquerade` permission in the target
channel.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time

import aiohttp

import stoat

# Imported as a module (not `from … import _CONTENT_LIMIT`) so a test patching
# `stoat_discord_bridge.services.stoat_service._CONTENT_LIMIT` is picked up by
# `receive()` at call time - the historical monkeypatch seam.
import stoat_discord_bridge.services.stoat_service as _stoat_pkg

from stoat_discord_bridge.models import CustomEmoji, StandardEdit, StandardMessage
from stoat_discord_bridge.services.base import PartialRelayError, ReceiverService
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
from stoat_discord_bridge.services.stoat_service.formatting import _download, _to_stoat_emoji
from stoat_discord_bridge.services.stoat_service.sender import StoatSenderService
from stoat_discord_bridge.storage.channel_mappings import ChannelMappingRepository
from stoat_discord_bridge.storage.emoji_mappings import EmojiMappingRepository
from stoat_discord_bridge.storage.role_mappings import RoleMappingRepository
from stoat_discord_bridge.storage.user_mappings import UserMappingRepository

logger = logging.getLogger(__name__)


class StoatReceiverService(ReceiverService):
    """Posts into Stoat "as" a remote (Discord/IRC) user via masquerade.

    Masquerade is a `send()` kwarg (`MessageMasquerade(name=, avatar=, color=)`),
    not a separate webhook-style API, so this reuses the already-connected
    sender client for the same server rather than needing its own identity
    to post through. The bot must have the `use_masquerade` permission in
    the target channel.
    """

    supports_reactions = True
    supports_emoji = True
    supports_pins = True
    supports_typing = True
    supports_edits = True

    # How long a single relayed typing indicator lingers before it's ended,
    # unless `trigger_typing` is called again first. Stoat/Revolt keeps a
    # BeginTyping alive until an explicit EndTyping, so - unlike Discord's
    # self-lapsing indicator - the receiver has to run a small keep-alive
    # loop and stop it once the origin user stops typing.
    _TYPING_LINGER = 6.0
    _TYPING_REFRESH = 2.5

    def __init__(
        self,
        sender: StoatSenderService,
        user_mappings: UserMappingRepository | None = None,
        channel_mappings: ChannelMappingRepository | None = None,
        role_mappings: RoleMappingRepository | None = None,
        emoji_mappings: EmojiMappingRepository | None = None,
        source_forwarding: bool = True,
        pronoun_forwarding: bool = True,
        color_forwarding: bool = True,
    ) -> None:
        self.connector_id = sender.connector_id
        self._sender = sender
        self._user_mappings = user_mappings
        self._channel_mappings = channel_mappings
        self._role_mappings = role_mappings
        self._emoji_mappings = emoji_mappings
        self._source_forwarding = source_forwarding
        self._pronoun_forwarding = pronoun_forwarding
        self._color_forwarding = color_forwarding
        # target_channel_id -> monotonic deadline the keep-alive loop stops at,
        # and the loop task itself (one per channel currently "typing").
        self._typing_until: dict[str, float] = {}
        self._typing_tasks: dict[str, asyncio.Task] = {}

    async def receive(self, message: StandardMessage, *, target_channel_id: str) -> list[str]:
        channel = self._sender.get_channel(target_channel_id, partial=True)
        sender_name = message.sender_name
        avatar_url = message.sender_avatar_url
        if self._user_mappings is not None:
            local_user_id = await self._user_mappings.find_linked_user_id(
                message.origin_connector_id, message.sender_user_id, self.connector_id
            )
            if local_user_id is not None:
                identity = await self._sender.get_masquerade_identity(local_user_id)
                if identity is not None:
                    sender_name, avatar_url = identity
        # Stoat caps a masquerade name at 32 chars; decorate_sender_name drops
        # the "[Source, pronouns]" suffix whole when the decorated name won't
        # fit rather than slicing it mid-token.
        sender_name = decorate_sender_name(
            sender_name,
            source=message.source_label if self._source_forwarding else None,
            pronouns=message.sender_pronouns if self._pronoun_forwarding else None,
            max_len=32,
        )
        masquerade = stoat.MessageMasquerade(
            name=sender_name,
            avatar=avatar_url,
            # The origin sender's name colour (issue #74). Setting it needs the
            # bot's `manage_roles` permission in the channel; a send rejected
            # for it is retried uncoloured below rather than lost.
            color=message.sender_color if self._color_forwarding else None,
        )
        # Re-upload the message's attachments as native Stoat files rather
        # than pasting their (often short-lived, signed) CDN URLs into the
        # text - see issue #39. Anything too large or unfetchable falls back
        # to an inlined URL below so it's not lost.
        files, undownloadable = await download_attachments(message.attachments)
        content = await self._rewrite_content(
            origin_connector_id=message.origin_connector_id,
            content_markdown=message.content_markdown,
            mentioned_users=message.mentioned_users,
        )
        if undownloadable:
            content = inline_attachment_urls(content, undownloadable)
        if content:
            chunks = chunk_content(content, _stoat_pkg._CONTENT_LIMIT)
        else:
            # A file-only message needs an empty content, not the zero-width
            # sentinel; keep the sentinel only when there's nothing to send.
            chunks = [""] if files else ["​"]
        ids: list[str] = []
        for index, chunk in enumerate(chunks):
            attach = files if files and index == len(chunks) - 1 else None
            attach_kw = {"attachments": list(attach)} if attach else {}
            logger.debug(
                "[stoat:%s] sending masqueraded message to channel %s as %r (avatar=%r, color=%r, files=%d): %r",
                self.connector_id,
                target_channel_id,
                masquerade.name,
                masquerade.avatar,
                masquerade.color,
                len(attach) if attach else 0,
                chunk,
            )
            try:
                sent = await channel.send(chunk, masquerade=masquerade, **attach_kw)
            except Exception as exc:
                if masquerade.color is None:
                    raise PartialRelayError(ids, exc) from exc
                # A masquerade colour needs `manage_roles` in the channel; if
                # that's what the server rejected, drop the colour and keep
                # relaying (this chunk and the rest of the split) rather than
                # losing the message.
                logger.warning(
                    "[stoat:%s] masqueraded send into %s rejected with colour %r (%s); retrying uncoloured",
                    self.connector_id,
                    target_channel_id,
                    masquerade.color,
                    exc,
                )
                masquerade = stoat.MessageMasquerade(name=masquerade.name, avatar=masquerade.avatar)
                try:
                    sent = await channel.send(chunk, masquerade=masquerade, **attach_kw)
                except Exception as exc2:
                    raise PartialRelayError(ids, exc2) from exc2
            logger.debug("[stoat:%s] masqueraded message sent, id=%s", self.connector_id, sent.id)
            ids.append(str(sent.id))
        # Best-effort, never fatal to the relay: keep a thread Category's
        # parent channel grouped at its top (see the sender method's docstring).
        await self._sender.group_parent_channel_with_threads(target_channel_id)
        return ids

    async def _rewrite_content(
        self, *, origin_connector_id: str, content_markdown: str | None, mentioned_users: dict[str, str]
    ) -> str:
        """Run the shared user / channel / role / emoji mention rewrites over a
        relayed message's text - used by `receive()` for the first post and by
        `edit_message()` to re-render it when the source is edited. Attachment
        handling stays in `receive()` (an edit doesn't re-sync files)."""
        content = content_markdown or ""
        if self._user_mappings is not None:
            content = await rewrite_mentions(
                content,
                origin_connector_id=origin_connector_id,
                target_connector_id=self.connector_id,
                target_kind="stoat",
                user_mappings=self._user_mappings,
                mentioned_users=mentioned_users,
            )
        if self._channel_mappings is not None:
            content = await rewrite_channel_mentions(
                content,
                origin_connector_id=origin_connector_id,
                target_connector_id=self.connector_id,
                target_kind="stoat",
                channel_mappings=self._channel_mappings,
            )
        if self._role_mappings is not None:
            content = await rewrite_role_mentions(
                content,
                origin_connector_id=origin_connector_id,
                target_connector_id=self.connector_id,
                target_kind="stoat",
                role_mappings=self._role_mappings,
            )
        if self._emoji_mappings is not None:
            content = await rewrite_emoji(
                content,
                origin_connector_id=origin_connector_id,
                target_connector_id=self.connector_id,
                target_kind="stoat",
                emoji_mappings=self._emoji_mappings,
            )
        return content

    async def edit_message(
        self, *, target_channel_id: str, target_message_ids: list[str], edit: StandardEdit
    ) -> None:
        """Re-render the edited text and patch every masqueraded post the
        original relay produced in this channel (Stoat lets the bot edit its
        own messages, masqueraded ones included). A relay split across N posts
        is matched chunk-for-post; a shortened edit blanks the leftover posts
        (zero-width space); an edit that grew past the existing posts drops the
        overflow. Best-effort: a post that's since been deleted, or an edit the
        server rejects, is skipped."""
        if not target_message_ids:
            return
        channel = self._sender.get_channel(target_channel_id, partial=True)
        content = await self._rewrite_content(
            origin_connector_id=edit.origin_connector_id,
            content_markdown=edit.new_content_markdown,
            mentioned_users=edit.mentioned_users,
        )
        chunks = chunk_content(content, _stoat_pkg._CONTENT_LIMIT) if content else []
        for index, message_id in enumerate(target_message_ids):
            body = chunks[index] if index < len(chunks) else "​"
            try:
                message = await channel.fetch_message(message_id)
                await message.edit(content=body)
            except Exception:
                logger.warning(
                    "[stoat:%s] couldn't edit relayed message %s in channel %s",
                    self.connector_id,
                    message_id,
                    target_channel_id,
                )

    async def add_reaction(self, *, target_channel_id: str, target_message_id: str, emoji: str | CustomEmoji) -> None:
        """Idempotent: skips the API call if the bridge bot has already
        reacted with this emoji on the target message (a second origin user
        reacting with the same emoji must not double-add)."""
        native = _to_stoat_emoji(emoji)
        if await self._bot_already_reacted(target_channel_id, target_message_id, native):
            return
        channel = self._sender.get_channel(target_channel_id, partial=True)
        try:
            message = await channel.fetch_message(target_message_id)
        except Exception:
            return  # message gone, or we can't see it - best-effort
        await message.react(native)

    async def remove_reaction(
        self, *, target_channel_id: str, target_message_id: str, emoji: str | CustomEmoji
    ) -> None:
        """Idempotent: skips the API call if the bridge bot isn't currently
        reacting with this emoji. `message.unreact(emoji)` with no `user=`
        removes only the caller's (bot's) own reaction."""
        native = _to_stoat_emoji(emoji)
        already = await self._bot_already_reacted(target_channel_id, target_message_id, native)
        if already is False:
            return
        channel = self._sender.get_channel(target_channel_id, partial=True)
        try:
            message = await channel.fetch_message(target_message_id)
        except Exception:
            return  # message gone, or we can't see it - best-effort
        await message.unreact(native)

    async def _bot_already_reacted(
        self, channel_id: str, message_id: str, native_emoji: str
    ) -> bool | None:
        """True/False if the bot's own reaction with `native_emoji` could be
        determined from a fresh fetch of the message, None if it couldn't
        (fetch failed / unknown self id) - callers treat None as "act anyway"."""
        self_id = self._sender.self_id
        if self_id is None:
            return None
        try:
            message = await self._sender.get_channel(channel_id, partial=True).fetch_message(message_id)
        except Exception:
            return None
        reactions = getattr(message, "reactions", None)
        if not isinstance(reactions, dict):
            return None
        return self_id in {str(u) for u in reactions.get(native_emoji, ())}

    async def set_pinned(self, *, target_channel_id: str, target_message_id: str, pinned: bool) -> None:
        channel = self._sender.get_channel(target_channel_id, partial=True)
        try:
            message = await channel.fetch_message(target_message_id)
        except Exception:
            return  # message gone, or we can't see it - best-effort
        if getattr(message, "pinned", None) == pinned:
            return  # already in the desired state - avoids a needless API call and echo
        try:
            if pinned:
                await message.pin()
            else:
                await message.unpin()
        except stoat.HTTPException:
            logger.warning(
                "[stoat:%s] couldn't %s message %s in channel %s",
                self.connector_id,
                "pin" if pinned else "unpin",
                target_message_id,
                target_channel_id,
            )

    async def trigger_typing(self, *, target_channel_id: str) -> None:
        """Show a typing indicator in the channel, attributed to the bridge
        bot (Stoat masquerade doesn't extend to typing). Extends the linger
        deadline and, if no keep-alive loop is running for this channel,
        starts one that re-sends BeginTyping until the deadline, then ends
        it. Best-effort: a bad channel / transient error just stops the loop."""
        self._typing_until[target_channel_id] = time.monotonic() + self._TYPING_LINGER
        task = self._typing_tasks.get(target_channel_id)
        if task is not None and not task.done():
            return
        self._typing_tasks[target_channel_id] = asyncio.ensure_future(
            self._keep_typing(target_channel_id)
        )

    async def stop_typing(self, *, target_channel_id: str) -> None:
        """End the typing indicator now (the origin user stopped typing before
        sending). Cancels the keep-alive loop and sends a final EndTyping.
        Best-effort: a bad channel / transient error is swallowed."""
        self._typing_until.pop(target_channel_id, None)
        task = self._typing_tasks.pop(target_channel_id, None)
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except BaseException:
                pass
        try:
            channel = self._sender.get_channel(target_channel_id, partial=True)
            await channel.end_typing()
        except Exception:
            pass

    async def _keep_typing(self, channel_id: str) -> None:
        channel = self._sender.get_channel(channel_id, partial=True)
        try:
            while time.monotonic() < self._typing_until.get(channel_id, 0.0):
                await channel.begin_typing()
                await asyncio.sleep(self._TYPING_REFRESH)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.debug("[stoat:%s] typing keep-alive for channel %s failed", self.connector_id, channel_id)
        finally:
            self._typing_until.pop(channel_id, None)
            self._typing_tasks.pop(channel_id, None)
            try:
                await channel.end_typing()
            except Exception:
                pass

    async def create_emoji(self, emoji: CustomEmoji) -> CustomEmoji | None:
        # Stoat emoji names may only contain lowercase ASCII letters, digits
        # and underscores (1-32 chars) - sanitise whatever the source platform
        # allowed down to that.
        name = re.sub(r"[^a-z0-9_]", "_", emoji.name.lower())[:32].strip("_") or "emoji"
        try:
            server = self._sender.get_server(self._sender.server_id, partial=True)
            image_bytes = await _download(emoji.image_url)
            upload = stoat.Upload.emoji(image_bytes, filename=f"{name}.png")
            created = await server.create_server_emoji(name, image=upload)
        except (stoat.HTTPException, aiohttp.ClientError) as exc:
            logger.warning("[stoat:%s] couldn't create emoji %r: %s", self.connector_id, emoji.name, exc)
            return None  # e.g. emoji slots full, name taken, image too large, network failure - skip this platform
        return CustomEmoji(
            native_id=str(created.id),
            name=created.name,
            image_url=created.image.url(),
            animated=getattr(created, "animated", emoji.animated),
        )
