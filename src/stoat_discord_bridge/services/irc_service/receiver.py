"""`IrcReceiverService`: outbound relay onto IRC.

IRC has no per-message identity override and no native attachment/markdown
support, so the receiver posts through the same connection as the sender,
prefixing each line with the remote user's name, inlining attachment URLs,
and stripping Markdown to plain text (`formatting.strip_markdown`).
"""

from __future__ import annotations

import logging

from stoat_discord_bridge.models import StandardMessage
from stoat_discord_bridge.services.base import PartialRelayError, ReceiverService
from stoat_discord_bridge.services.formatting import chunk_content, render_discord_timestamps, strip_markdown
from stoat_discord_bridge.services.irc_service.formatting import _LINE_LIMIT, _synthetic_message_id
from stoat_discord_bridge.services.irc_service.sender import IrcSenderService
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


class IrcReceiverService(ReceiverService):
    def __init__(
        self,
        sender: IrcSenderService,
        user_mappings: UserMappingRepository | None = None,
        enable_local_user_masquerade: bool = True,
        channel_mappings: ChannelMappingRepository | None = None,
        role_mappings: RoleMappingRepository | None = None,
        emoji_mappings: EmojiMappingRepository | None = None,
        source_forwarding: bool = True,
        pronoun_forwarding: bool = True,
    ) -> None:
        self.connector_id = sender.connector_id
        self._sender = sender
        self._user_mappings = user_mappings
        self._enable_local_user_masquerade = enable_local_user_masquerade
        self._channel_mappings = channel_mappings
        self._role_mappings = role_mappings
        self._emoji_mappings = emoji_mappings
        self._source_forwarding = source_forwarding
        self._pronoun_forwarding = pronoun_forwarding

    async def receive(self, message: StandardMessage, *, target_channel_id: str) -> list[str]:
        # IRC has no markup - reduce Discord/Stoat Markdown to plain text
        # before anything else, while the content is still just the message
        # body (doing it after the attachment URLs are inlined would risk an
        # underscore/asterisk in a CDN link being read as emphasis).
        content = strip_markdown(message.content_markdown)
        # IRC has no native attachments - inline each attachment URL (a
        # Discord/Stoat CDN link) as its own line so an image-only message
        # isn't relayed blank. Done inline rather than via
        # formatting.inline_attachment_urls(), whose empty-message sentinel
        # would put a zero-width space on the wire. (Discord/Stoat re-upload
        # these as native files instead - see formatting.download_attachments.)
        if message.attachments:
            extra = "\n".join(a.url for a in message.attachments if a.url)
            if extra:
                content = f"{content}\n{extra}" if content else extra
        # Discord/Stoat <t:...> dynamic timestamps have no IRC equivalent - render
        # them to plain text (relative styles are relative to right now, i.e. when
        # this handler runs).
        content = render_discord_timestamps(content)
        sender_name = message.sender_name
        if self._user_mappings is not None:
            content = await rewrite_mentions(
                content,
                origin_connector_id=message.origin_connector_id,
                target_connector_id=self.connector_id,
                target_kind="irc",
                user_mappings=self._user_mappings,
                mentioned_users=message.mentioned_users,
            )
            if self._enable_local_user_masquerade:
                # A linked sender's user_id on IRC IS the nick (see
                # storage/user_mappings.py's UserMapping.user_id docstring), so
                # unlike Discord/Stoat this needs no further identity lookup.
                local_nick = await self._user_mappings.find_linked_user_id(
                    message.origin_connector_id, message.sender_user_id, self.connector_id
                )
                if local_nick is not None:
                    logger.debug(
                        "[irc:%s] resolved local user masquerade identity for %s: nick=%r",
                        self.connector_id,
                        message.sender_user_id,
                        local_nick,
                    )
                    sender_name = local_nick
            else:
                logger.debug(
                    "[irc:%s] local user masquerade disabled (enable_local_user_masquerade=false), "
                    "not resolving local nick for sender %s",
                    self.connector_id,
                    message.sender_user_id,
                )
        if self._channel_mappings is not None:
            content = await rewrite_channel_mentions(
                content,
                origin_connector_id=message.origin_connector_id,
                target_connector_id=self.connector_id,
                target_kind="irc",
                channel_mappings=self._channel_mappings,
                mentioned_channels=message.mentioned_channels,
            )
        if self._role_mappings is not None:
            content = await rewrite_role_mentions(
                content,
                origin_connector_id=message.origin_connector_id,
                target_connector_id=self.connector_id,
                target_kind="irc",
                role_mappings=self._role_mappings,
            )
        if self._emoji_mappings is not None:
            content = await rewrite_emoji(
                content,
                origin_connector_id=message.origin_connector_id,
                target_connector_id=self.connector_id,
                target_kind="irc",
                emoji_mappings=self._emoji_mappings,
            )
        if not content.strip():
            # A synced message with no textual content (after attachment
            # inlining and mention/timestamp rewrites) has nothing to show on
            # IRC. This is how IRC ignores pin/unpin notifications, which
            # Discord/Stoat relay as content-less messages - IRC has no
            # message-pin concept, so it just drops them.
            logger.debug(
                "[irc:%s] dropping content-less synced message into %s", self.connector_id, target_channel_id
            )
            return []
        # IRC has no per-message identity override, so the remote user's
        # details ride in the `<...>` line tag: `<nick>` normally, and with
        # source_forwarding / pronoun_forwarding on (issue #54),
        # `<nick, Discord, she/her>`.
        tag_parts = [sender_name]
        if self._source_forwarding and message.source_label:
            tag_parts.append(message.source_label)
        if self._pronoun_forwarding and message.sender_pronouns:
            tag_parts.append(message.sender_pronouns)
        prefix = f"<{', '.join(tag_parts)}> "
        limit = max(1, _LINE_LIMIT - len(prefix))
        ids: list[str] = []
        for line in content.splitlines() or [""]:
            for chunk in chunk_content(line, limit):
                try:
                    self._sender.connection.privmsg(target_channel_id, f"{prefix}{chunk}")
                except Exception as exc:
                    raise PartialRelayError(ids, exc) from exc
                # IRC has no native message ID to echo back; synthesize one
                # (same scheme as inbound messages) so each post still gets a
                # distinct, non-colliding sync-tracking key.
                ids.append(_synthetic_message_id(target_channel_id, message.sender_name, chunk))
        return ids
