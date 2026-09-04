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

A mentioned user with no mapping to the target connector is expanded to a
plain `@Display Name` (the name on the origin, carried on the
`StandardMessage.mentioned_users` map) so the target doesn't just see a
raw `<@id>` token (issue #56); if even that name can't be recovered the
mention is left exactly as it appeared - never dropped or replaced with
something meaningless. Neither regex needs to know which connector
authored the message: Discord's numeric-id and Stoat's 26-char-ULID
mention shapes never collide with each other, so both are always tried.
"""

from __future__ import annotations

import re

from stoat_discord_bridge.storage.channel_mappings import ChannelMappingRepository
from stoat_discord_bridge.storage.emoji_mappings import EmojiMappingRepository
from stoat_discord_bridge.storage.role_mappings import RoleMappingRepository
from stoat_discord_bridge.storage.user_mappings import UserMappingRepository

_DISCORD_MENTION = re.compile(r"<@!?(\d+)>")
_STOAT_MENTION = re.compile(r"<@([A-Za-z0-9]{26})>")

_DISCORD_CHANNEL_MENTION = re.compile(r"<#(\d+)>")
_STOAT_CHANNEL_MENTION = re.compile(r"<#([A-Za-z0-9]{26})>")

_DISCORD_CUSTOM_EMOJI = re.compile(r"<(a?):(\w+):(\d+)>")
# Stoat renders a custom emoji as its bare 26-char ULID between colons;
# `:word:` shortcodes (Unicode emoji) never match this and are left alone.
_STOAT_CUSTOM_EMOJI = re.compile(r":([0-9A-Za-z]{26}):")

_DISCORD_ROLE_MENTION = re.compile(r"<@&(\d+)>")
# Stoat/Revolt role-mention syntax - TODO: unverified against a live server,
# same caveat as the user-mention shape above.
_STOAT_ROLE_MENTION = re.compile(r"<%([A-Za-z0-9]{26})>")


async def rewrite_channel_mentions(
    content: str,
    *,
    origin_connector_id: str,
    target_connector_id: str,
    target_kind: str,
    channel_mappings: ChannelMappingRepository,
    mentioned_channels: dict[str, str] | None = None,
) -> str:
    """Rewrite a `<#channel-id>` mention of a cross-connector-linked channel
    into the target connector's own copy of that channel - its native
    `<#id>` syntax on Discord/Stoat, or `#name` on IRC.

    A mention of a channel with no mapping to the target is expanded to a
    plain `#channel-name` using `mentioned_channels` (origin native channel
    id -> its name on the origin) so the target doesn't just see a raw `<#id>`
    token that renders as a dead id (issue #84, mirroring the unlinked-user
    rule); a mention still not covered by that map is left exactly as it
    appeared. Both id shapes are always tried; Discord's numeric ids and
    Stoat's 26-char ULIDs never collide."""
    mentioned_channels = mentioned_channels or {}
    for pattern in (_DISCORD_CHANNEL_MENTION, _STOAT_CHANNEL_MENTION):
        for match in list(pattern.finditer(content)):
            channel_id = match.group(1)
            target = None
            bridge_group = await channel_mappings.get_bridge_group(origin_connector_id, channel_id)
            if bridge_group is not None:
                target = next(
                    (
                        m
                        for m in await channel_mappings.get_mapped_channels(bridge_group)
                        if m.connector_id == target_connector_id
                    ),
                    None,
                )
            if target is not None:
                if target_kind == "irc":
                    # On IRC the channel id literally *is* the `#channel` name.
                    replacement = target.channel_id if target.channel_id.startswith("#") else f"#{target.channel_id}"
                else:
                    replacement = f"<#{target.channel_id}>"
            elif channel_id in mentioned_channels:
                # No link to the target - fall back to a readable `#name`.
                # Defanged like the unlinked-user expansion: a channel name is
                # attacker-controllable on some platforms, and the bridge sets
                # no `allowed_mentions` on its sends.
                replacement = _defang_mentions("#" + mentioned_channels[channel_id])
            else:
                continue
            content = content.replace(match.group(0), replacement)
    return content


async def rewrite_role_mentions(
    content: str,
    *,
    origin_connector_id: str,
    target_connector_id: str,
    target_kind: str,
    role_mappings: RoleMappingRepository,
    mentioned_roles: dict[str, str] | None = None,
) -> str:
    """Rewrite a `<@&role-id>` (Discord) / `<%role-id>` (Stoat) mention of a
    cross-connector-linked role into the target connector's own copy of that
    role - native syntax on Discord/Stoat, or `@name` on IRC.

    `mentioned_roles` maps an origin native role id to that role's name on the
    origin. A mention of a role with no mapping to the target is expanded to a
    plain `@Role Name` using this map rather than relayed as the raw id token
    (issue #4 - the role counterpart of the issue-#56 user-mention fix); a
    mention still not covered by the map is left exactly as it appeared. Both
    id shapes are always tried; Discord's numeric ids and Stoat's 26-char
    ULIDs never collide."""
    mentioned_roles = mentioned_roles or {}
    # Unlinked mentions we can name are expanded only after the linked-role
    # rewrite below, so an expanded `@Name` can't collide with a real
    # `<%id>` / `<@&id>` we just wrote (and the expansion is defanged).
    pending_expansions: list[tuple[str, str]] = []
    for pattern in (_DISCORD_ROLE_MENTION, _STOAT_ROLE_MENTION):
        for match in list(pattern.finditer(content)):
            target = None
            bridge_group = await role_mappings.get_bridge_group(origin_connector_id, match.group(1))
            if bridge_group is not None:
                target = next(
                    (
                        m
                        for m in await role_mappings.get_mapped_roles(bridge_group)
                        if m.connector_id == target_connector_id
                    ),
                    None,
                )
            if target is None:
                if match.group(1) in mentioned_roles:
                    pending_expansions.append((match.group(0), mentioned_roles[match.group(1)]))
                continue
            if target_kind == "discord":
                replacement = f"<@&{target.role_id}>"
            elif target_kind == "stoat":
                replacement = f"<%{target.role_id}>"
            else:
                replacement = f"@{target.role_name}"
            content = content.replace(match.group(0), replacement)
    for token, name in pending_expansions:
        content = content.replace(token, _defang_mentions("@" + name))
    return content


async def rewrite_emoji(
    content: str,
    *,
    origin_connector_id: str,
    target_connector_id: str,
    target_kind: str,
    emoji_mappings: EmojiMappingRepository,
    mentioned_emoji: dict[str, str] | None = None,
) -> str:
    """Rewrite an inline custom-emoji reference - `<:name:id>` / `<a:name:id>`
    (Discord) or `:26-char-ULID:` (Stoat) - into the target connector's own
    linked copy of that emoji: `<:name:id>` on Discord, `:id:` on Stoat.

    An emoji with no link to the target connector falls back to a plain
    `:name:` shortcode instead of the raw token, which isn't valid emoji
    syntax on the target and would otherwise render as literal text exposing
    a bare id (issue #87, the emoji counterpart of the #56/#4/#84 mention
    fallbacks). Discord's token carries the name inline (`match.group(2)`);
    Stoat's bare-ULID token doesn't, so that name comes from `mentioned_emoji`
    (an origin id -> name map, best-effort - see the Stoat sender) or, failing
    that, `EmojiMappingRepository.find_name` (recovers a name if the emoji is
    in some mapping group even without a link to *this* target). On IRC a
    still-unnamed Stoat emoji is removed entirely (unchanged from before);
    on Discord/Stoat it falls back to a generic `:emoji:` marker rather than
    a bare id. Both id shapes are always tried; a Discord numeric id and a
    Stoat ULID never collide.

    Both patterns are matched against the *original* `content` up front,
    before any substitution: a Discord-target/Stoat-target replacement for a
    linked emoji renders as a bare `:id:`, which is exactly the Stoat token
    shape, so matching the second pattern against already-rewritten text
    would re-process the first loop's own output."""
    mentioned_emoji = mentioned_emoji or {}
    discord_matches = list(_DISCORD_CUSTOM_EMOJI.finditer(content))
    stoat_matches = list(_STOAT_CUSTOM_EMOJI.finditer(content))
    for match in discord_matches:
        ref = await emoji_mappings.find_equivalent_ref(
            origin_connector_id, match.group(3), target_connector_id
        )
        if ref is not None:
            replacement = _render_emoji(target_kind, ref.emoji_id, ref.name or match.group(2))
        else:
            # No link to the target - the name is right there in the Discord
            # token, so fall back to a readable shortcode on every target
            # kind rather than leaving the raw, unrenderable `<:name:id>`.
            replacement = f":{match.group(2)}:"
        content = content.replace(match.group(0), replacement)
    for match in stoat_matches:
        ref = await emoji_mappings.find_equivalent_ref(
            origin_connector_id, match.group(1), target_connector_id
        )
        if ref is not None:
            replacement = _render_emoji(target_kind, ref.emoji_id, ref.name)
        else:
            name = mentioned_emoji.get(match.group(1)) or await emoji_mappings.find_name(
                origin_connector_id, match.group(1)
            )
            if name:
                replacement = f":{name}:"
            elif target_kind == "irc":
                replacement = ""
            else:
                replacement = ":emoji:"
        content = content.replace(match.group(0), replacement)
    return content


def _render_emoji(target_kind: str, emoji_id: str, name: str | None) -> str:
    if target_kind == "discord":
        return f"<:{name or 'emoji'}:{emoji_id}>"
    if target_kind == "stoat":
        return f":{emoji_id}:"
    return f":{name or emoji_id}:"


async def rewrite_mentions(
    content: str,
    *,
    origin_connector_id: str,
    target_connector_id: str,
    target_kind: str,
    user_mappings: UserMappingRepository,
    mentioned_users: dict[str, str] | None = None,
) -> str:
    """`target_kind` is one of "discord" / "stoat" / "irc" - the caller
    (a receiver's `receive()`) already knows its own kind.

    `mentioned_users` maps an origin native user id to that user's display
    name on the origin. A `<@id>` mention of a user with no /link-user link
    to the target is expanded to a plain `@Display Name` using this map
    rather than relayed as the raw id token (issue #56); a mention still not
    covered by the map is left exactly as it appeared."""
    mentioned_users = mentioned_users or {}
    # Unlinked `<@id>` mentions we can name are expanded only *after* the
    # plain-word nick scan below, so an injected display name can't itself be
    # re-read as a nick mention - the raw token is inert to that scan.
    pending_expansions: list[tuple[str, str]] = []
    for pattern in (_DISCORD_MENTION, _STOAT_MENTION):
        for match in list(pattern.finditer(content)):
            target_id, target_name = await _resolve_target(
                origin_connector_id, match.group(1), target_connector_id, user_mappings
            )
            if target_id is not None:
                content = content.replace(match.group(0), _render_mention(target_kind, target_id, target_name))
            elif match.group(1) in mentioned_users:
                pending_expansions.append((match.group(0), mentioned_users[match.group(1)]))

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

    for token, name in pending_expansions:
        content = content.replace(token, _defang_mentions("@" + name))

    return content


_ZWSP = "\u200b"
_PING_KEYWORD = re.compile(r"@(everyone|here)\b")


def _defang_mentions(text: str) -> str:
    """Neutralise anything in a `@<origin display name>` expansion that a
    target could parse as a live mention before it's spliced into relayed
    text - an `@everyone` / `@here` mass ping (whether it's the leading `@`
    we added or one inside the name) and any stray `<@id>` / `<#id>` /
    `<@&id>` / `<%id>` token - by wedging a zero-width space in after the
    sigil. It still reads the same; it just can't ping. (The bridge sets no
    `allowed_mentions` on its webhook/masquerade sends, so an un-defanged
    `@everyone` in a display name would be a live mass ping.)"""
    text = _PING_KEYWORD.sub(rf"@{_ZWSP}\1", text)
    return text.replace("<@", f"<{_ZWSP}@").replace("<#", f"<{_ZWSP}#").replace("<%", f"<{_ZWSP}%")


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
