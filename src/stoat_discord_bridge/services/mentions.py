"""Rewrites @mentions of cross-connector-linked users into the target
connector's native mention syntax when relaying a message (see
storage/user_mappings.py for how links are created, via /link-user).

Discord and Stoat mentions are both structured (`<@id>`) - Stoat's format
is assumed to mirror Discord's per the same revolt.py lineage the rest of
this Stoat integration is modeled on; TODO: unverified against a live
server, same caveat as elsewhere in this codebase's Stoat integration.
IRC has no structured mention syntax at all - a "mention" there is just
typing someone's nick as a plain word in the message, so the IRC-origin
direction is a best-effort, word-boundary match against nicks that are
actually linked (false positives are possible if a word incidentally
matches a linked nick), and the IRC-target direction is just substituting
the plain nick text.

A mentioned user with no mapping to the target connector is left exactly
as it appeared in the original message - never dropped or replaced with
something meaningless. Neither regex needs to know which connector
authored the message: Discord's numeric-id and Stoat's 26-char-ULID
mention shapes never collide with each other, so both are always tried.
"""

from __future__ import annotations

import re

from stoat_discord_bridge.storage.user_mappings import UserMappingRepository

_DISCORD_MENTION = re.compile(r"<@!?(\d+)>")
_STOAT_MENTION = re.compile(r"<@([A-Za-z0-9]{26})>")


async def rewrite_mentions(
    content: str,
    *,
    origin_connector_id: str,
    target_connector_id: str,
    target_kind: str,
    user_mappings: UserMappingRepository,
) -> str:
    """`target_kind` is one of "discord" / "stoat" / "irc" - the caller
    (a receiver's `receive()`) already knows its own kind."""
    for pattern in (_DISCORD_MENTION, _STOAT_MENTION):
        for match in list(pattern.finditer(content)):
            target_id, target_name = await _resolve_target(
                origin_connector_id, match.group(1), target_connector_id, user_mappings
            )
            if target_id is not None:
                content = content.replace(match.group(0), _render_mention(target_kind, target_id, target_name))

    # IRC-origin (and, harmlessly, any other origin) plain-word nick scan:
    # any linked identity whose *own* user_id literally appears in the text
    # as a standalone word is treated as a mention of that user.
    for link in await user_mappings.get_all_for_connector(origin_connector_id):
        pattern = re.compile(rf"\b{re.escape(link.user_id)}\b")
        if not pattern.search(content):
            continue
        target_id, target_name = await _resolve_target(
            origin_connector_id, link.user_id, target_connector_id, user_mappings
        )
        if target_id is not None:
            content = pattern.sub(_render_mention(target_kind, target_id, target_name), content)

    return content


async def _resolve_target(
    origin_connector_id: str, origin_user_id: str, target_connector_id: str, user_mappings: UserMappingRepository
) -> tuple[str | None, str | None]:
    link_group = await user_mappings.get_link_group(origin_connector_id, origin_user_id)
    if link_group is None:
        return None, None
    for mapping in await user_mappings.get_mapped_users(link_group):
        if mapping.connector_id == target_connector_id:
            return mapping.user_id, mapping.display_name
    return None, None


def _render_mention(target_kind: str, user_id: str, display_name: str | None) -> str:
    if target_kind == "discord":
        return f"<@{user_id}>"
    if target_kind == "stoat":
        return f"<@{user_id}>"
    if target_kind == "irc":
        return user_id  # for IRC, user_id IS the nick
    return display_name or user_id
