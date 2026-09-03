"""Reaction / emoji / role / typing / channel sync event handlers for Stoat.

The `_StoatClient` gateway callbacks (`on_message_react`, `on_channel_update`,
`on_server_member_update`, …) all land here, turning a native Stoat event
into the bridge's `Standard*` shape, plus the receiver-facing hooks the
`RoleSyncCoordinator` calls back in (`grant_role`, `rename_role`,
`set_channel_role_permission`, …). Composed into `StoatSenderService`.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace

import stoat

from stoat_discord_bridge.models import (
    CustomEmoji,
    StandardEmojiCreated,
    StandardEmojiDeleted,
    StandardReaction,
    StandardTyping,
)
from stoat_discord_bridge.services.role_sync import (
    neutral_to_stoat_pair,
    stoat_override_to_neutral,
)
from stoat_discord_bridge.services.stoat_service.formatting import _display_name, _parse_stoat_emoji

logger = logging.getLogger(__name__)


class StoatSyncMixin:
    """Sync-event half of `StoatSenderService`."""

    async def _handle_channel_create(self, channel) -> None:
        """`_StoatClient.on_server_channel_create`'s target - auto-syncs a
        newly-created channel into every other connector's own linked
        Category, if the Category this channel appeared in on this server is
        itself linked via `/link-category`. Best-effort: never lets a sync
        failure or an unrelated channel (wrong server, no Category, Category
        linking not configured) propagate."""
        if self._category_linker is None or getattr(channel, "server_id", None) != self.server_id:
            return
        try:
            category = channel.category
        except Exception:
            category = None
        if category is None:
            return
        try:
            await self._category_linker.sync_new_channel(
                local_connector=self.connector_id,
                local_category_id=str(category.id),
                channel_id=str(channel.id),
                channel_name=channel.name,
            )
        except Exception:
            logger.exception("[stoat:%s] failed to auto-sync new channel %s", self.connector_id, channel.id)

    async def _handle_typing(self, event, *, active: bool = True) -> None:
        """`stoat.events.ChannelStart/StopTypingEvent`: someone started
        (`active`) or stopped (`not active`) typing. Relay it across the bridge
        (BridgeCoordinator scopes it to a mapped channel). Dropped for the
        bridge bot's own typing, which the receiver-side keep-alive would
        otherwise echo back here."""
        if self._on_typing is None:
            return
        user_id = str(getattr(event, "user_id", "") or "")
        channel_id = str(getattr(event, "channel_id", "") or "")
        if not user_id or not channel_id or user_id == self._self_id:
            return
        name = user_id
        try:
            user = self._client.get_user(user_id)
        except Exception:
            user = None
        if user is not None:
            name = _display_name(user) or user_id
        await self._on_typing(
            StandardTyping(
                origin_connector_id=self.connector_id,
                origin_channel_id=channel_id,
                sender_name=name,
                sender_user_id=user_id,
                active=active,
            )
        )

    async def _handle_message_react(self, event, *, added: bool) -> None:
        """`stoat.events.MessageReactEvent` / `MessageUnreactEvent`: someone
        added/removed a reaction. `event` carries `.channel_id`, `.message_id`,
        `.user_id`, `.emoji` (Unicode char or 26-char custom-emoji ULID) and
        an optional `.message` (state before the event, when cached)."""
        if self._on_reaction is None or str(event.user_id) == self._self_id:
            return  # the bridge's own mirrored reaction landing back here - drop it, don't re-relay
        emoji = _parse_stoat_emoji(event.emoji)
        if emoji is None:
            logger.debug(
                "[stoat:%s] dropping reaction with non-portable builtin emoji %r", self.connector_id, event.emoji
            )
            return  # a Stoat/Revolt builtin shortcode - no equivalent on other connectors
        message = getattr(event, "message", None)
        reactions = getattr(message, "reactions", None) if message is not None else None
        count = len(reactions.get(event.emoji, ())) if isinstance(reactions, dict) else None
        await self._on_reaction(
            StandardReaction(
                origin_connector_id=self.connector_id,
                origin_channel_id=str(event.channel_id),
                origin_message_id=str(event.message_id),
                emoji=emoji,
                added=added,
                origin_reactor_count=count,
            )
        )

    async def _handle_emoji_create(self, emoji) -> None:
        """`stoat.events.ServerEmojiCreateEvent.emoji` (a `stoat.ServerEmoji`):
        a custom emoji was added to a server - mirror it onto every other
        connector (see BridgeCoordinator.handle_emoji_created)."""
        logger.debug("[stoat:%s] emoji created id=%s name=%r", self.connector_id, emoji.id, emoji.name)
        if self._on_emoji_created is None:
            return
        if getattr(emoji, "creator_id", None) is not None and str(emoji.creator_id) == self._self_id:
            return  # the bridge's own mirrored emoji landing back here - drop it, don't re-mirror
        await self._on_emoji_created(
            StandardEmojiCreated(
                origin_connector_id=self.connector_id,
                emoji=CustomEmoji(
                    native_id=str(emoji.id),
                    name=emoji.name,
                    image_url=emoji.image.url(),
                    animated=getattr(emoji, "animated", False),
                ),
            )
        )

    async def _handle_emoji_delete(self, emoji_id) -> None:
        """`stoat.events.ServerEmojiDeleteEvent`: a custom emoji was removed.
        Deletions are never mirrored onto other platforms (see
        BridgeCoordinator.handle_emoji_deleted), so - unlike create - there's
        no self-mirrored echo to filter out here."""
        logger.debug("[stoat:%s] emoji deleted id=%s", self.connector_id, emoji_id)
        if self._on_emoji_deleted is None or emoji_id is None:
            return
        await self._on_emoji_deleted(
            StandardEmojiDeleted(origin_connector_id=self.connector_id, native_id=str(emoji_id))
        )

    async def _handle_member_update(self, event) -> None:
        """A server member changed - diff their role id set for role
        auto-grant. `event.before` / `event.after` are Optional[Member], each
        with `.id` / `.server_id` / `.role_ids` (verified against stoat.py
        1.2.1); `after is None` when the member wasn't cached, so nothing to
        diff. `event.member` (PartialMember) is the id fallback."""
        if self._on_member_roles_changed is None:
            return
        before = getattr(event, "before", None)
        after = getattr(event, "after", None)
        if after is None:
            return
        if getattr(after, "server_id", self.server_id) != self.server_id:
            return
        before_ids = {str(r) for r in (getattr(before, "role_ids", []) or [])} if before is not None else set()
        after_ids = {str(r) for r in (getattr(after, "role_ids", []) or [])}
        added = after_ids - before_ids
        removed = before_ids - after_ids
        if not added and not removed:
            return
        user_id = str(getattr(after, "id", "") or getattr(event, "member", SimpleNamespace(id="")).id)
        await self._on_member_roles_changed(self.connector_id, user_id, added, removed)

    async def _handle_role_update(self, event) -> None:
        """A role was created or edited - propagate a rename to linked
        copies. `RawServerRoleUpdateEvent` is the combined create+update
        event: `event.old_role` (Optional[Role]) is None both when the role
        was just created and when the server isn't cached, so a None
        `old_role` is skipped either way (roles aren't auto-mirrored on
        creation). `event.new_role` falls back to `event.role` (PartialRole).
        Verified against stoat.py 1.2.1."""
        if self._on_role_renamed is None:
            return
        old_role = getattr(event, "old_role", None)
        new_role = getattr(event, "new_role", None) or getattr(event, "role", None)
        if old_role is None or new_role is None:
            return
        if getattr(old_role, "name", None) == getattr(new_role, "name", None):
            return
        role_id = str(getattr(new_role, "id", "") or getattr(old_role, "id", ""))
        if not role_id:
            return
        await self._on_role_renamed(self.connector_id, role_id, new_role.name)

    async def _handle_role_delete(self, event) -> None:
        if self._on_role_deleted is None:
            return
        if getattr(event, "server_id", self.server_id) != self.server_id:
            return
        role_id = str(getattr(event, "role_id", "") or getattr(getattr(event, "role", None), "id", ""))
        if role_id:
            await self._on_role_deleted(self.connector_id, role_id)

    async def _handle_channel_update(self, event) -> None:
        """A channel was edited - diff its role permission overrides for
        permission mirroring. `event.before` / `event.after` are
        Optional[Channel]; a server channel exposes `.role_permissions` as
        `dict[str, PermissionOverride]` (each override with `.allow` / `.deny`).
        `event.channel` (PartialChannel) is the fallback when `after` is None -
        it carries only the fields that changed. Verified against stoat.py
        1.2.1."""
        if self._on_channel_role_permission_changed is None:
            return
        before = getattr(event, "before", None)
        after = getattr(event, "after", None) or getattr(event, "channel", None)
        if after is None:
            return
        before_rp = dict(getattr(before, "role_permissions", {}) or {}) if before is not None else {}
        after_rp = dict(getattr(after, "role_permissions", {}) or {})
        for role_id in set(before_rp) | set(after_rp):
            b = before_rp.get(role_id)
            a = after_rp.get(role_id)
            if b is a or (b is not None and a is not None and getattr(b, "to_dict", lambda: b)() == getattr(a, "to_dict", lambda: a)()):
                continue
            allow = getattr(a, "allow", None)
            deny = getattr(a, "deny", None)
            override = stoat_override_to_neutral(allow, deny) if a is not None else stoat_override_to_neutral(None, None)
            await self._on_channel_role_permission_changed(
                self.connector_id, str(getattr(after, "id", "")), str(role_id), override, is_category=False
            )

    async def get_channel_role_permission(self, channel_id: str, role_id: str):
        try:
            channel = self._client.get_channel(channel_id, partial=False)
            override = (getattr(channel, "role_permissions", {}) or {}).get(role_id)
        except Exception:
            return None
        if override is None:
            return stoat_override_to_neutral(None, None)
        return stoat_override_to_neutral(getattr(override, "allow", None), getattr(override, "deny", None))

    async def set_channel_role_permission(self, channel_id: str, role_id: str, override) -> None:
        try:
            channel = self._client.get_channel(channel_id, partial=False)
        except Exception:
            return
        if channel is None or not hasattr(channel, "set_role_permissions"):
            return
        current = (getattr(channel, "role_permissions", {}) or {}).get(role_id)
        if current is not None:
            cur_neutral = stoat_override_to_neutral(getattr(current, "allow", None), getattr(current, "deny", None))
            if cur_neutral == override:
                return
        allow, deny = neutral_to_stoat_pair(override, stoat.Permissions)
        try:
            await channel.set_role_permissions(role_id, allow=allow, deny=deny)
        except Exception:
            logger.exception(
                "[stoat:%s] perm sync: set on channel %s role %s failed", self.connector_id, channel_id, role_id
            )

    async def rename_role(self, role_id: str, new_name: str) -> None:
        """Idempotent - skips the edit if the role already has that name."""
        role = self._role_by_id(role_id)
        if role is None:
            try:
                server = self._client.get_server(self.server_id, partial=False)
                if not isinstance(server, stoat.Server):
                    server = await self._client.fetch_server(self.server_id)
                role = next((r for r in self._roles_of(server) if str(getattr(r, "id", "")) == role_id), None)
            except Exception:
                role = None
        if role is None or getattr(role, "name", None) == new_name:
            return
        try:
            await role.edit(name=new_name)
        except Exception:
            logger.exception("[stoat:%s] role sync: rename of %s failed", self.connector_id, role_id)

    async def grant_role(self, user_id: str, role_id: str) -> None:
        """Idempotent (no-op if the member already has the role) so the
        grant echo doesn't loop. Best-effort. Note stoat.py's Member.edit
        REPLACES the whole role list - this is a read-modify-write."""
        await self._edit_member_roles(user_id, role_id, add=True)

    async def revoke_role(self, user_id: str, role_id: str) -> None:
        await self._edit_member_roles(user_id, role_id, add=False)

    async def _edit_member_roles(self, user_id: str, role_id: str, *, add: bool) -> None:
        try:
            member = await self._client.get_server(self.server_id, partial=True).fetch_member(user_id)
        except Exception:
            logger.warning("[stoat:%s] role sync: couldn't fetch member %s", self.connector_id, user_id)
            return
        current = [str(r) for r in (getattr(member, "role_ids", []) or [])]
        has = role_id in current
        if has == add:
            return
        if add:
            current.append(role_id)
        else:
            current = [r for r in current if r != role_id]
        try:
            await member.edit(roles=current)
        except Exception:
            logger.exception(
                "[stoat:%s] role sync: %s role %s for %s failed",
                self.connector_id,
                "add" if add else "remove",
                role_id,
                user_id,
            )
