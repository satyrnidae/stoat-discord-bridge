"""Linked-user local identity resolution for the Stoat connector -
`get_masquerade_identity`, the largest single method in `lookups.py`, split
out on its own (~90 lines) since it's a distinct concern from plain id/name
resolution: turning a `/link-user`-linked remote user into the
(display_name, avatar_url) StoatReceiverService masquerades them as locally.
"""

from __future__ import annotations

import logging

from stoat_discord_bridge.services.stoat_service.formatting import _avatar_url, _display_name

logger = logging.getLogger(__name__)


class _IdentityMixin:
    """Local-identity resolution half of `StoatLookupsMixin`."""

    async def get_masquerade_identity(self, user_id: str) -> tuple[str, str | None] | None:
        """Best-effort (display_name, avatar_url) for `user_id` as a member
        of this connector's own Stoat server (`self.server_id` - there's
        exactly one per connector, see StoatConnectorConfig), used by
        StoatReceiverService to masquerade a linked (/link-user) sender as
        their local Stoat identity instead of their remote one. Prefers the
        per-server Member (whose nickname/avatar override applies) over the
        global User, same preference `_resolve_avatar_url` gives a message's
        own author below - deliberately keyed off the connector's own
        server_id rather than derived from a `get_channel(partial=True)`
        object, which - being partial - isn't guaranteed to carry a
        populated server_id at all. Returns None if `user_id` can't be
        resolved to a real name at all (never falls back to displaying the
        bare id - the caller should keep the remote identity instead)."""
        if not self._config.enable_local_user_masquerade:
            logger.debug(
                "[stoat:%s] local user masquerade disabled (enable_local_user_masquerade=false), "
                "not resolving local identity for user %s",
                self.connector_id,
                user_id,
            )
            return None
        try:
            member = await self._client.get_server(self.server_id, partial=True).fetch_member(user_id)
        except Exception as exc:
            logger.debug(
                "[stoat:%s] couldn't fetch server member %s for local user masquerade: %s",
                self.connector_id,
                user_id,
                exc,
            )
            member = None
        # A member's own explicit avatar override (server_avatar, or a
        # global avatar carried on an already-fully-resolved Member) - kept
        # separate from _avatar_url()'s default-avatar fallback below, since
        # that fallback is a generic placeholder, not a real per-user value
        # worth preferring over one fetch_user() will actually resolve.
        member_avatar_override = (
            (getattr(member, "server_avatar", None) or getattr(member, "avatar", None)) if member is not None else None
        )
        name = _display_name(member) if member is not None else ""
        user = None
        if not name:
            # stoat.py's Member.name/display_name properties (confirmed
            # against the installed package, server.py) silently return
            # ""/None rather than the member's real username whenever the
            # Member's `internal_user` reference isn't a locally cached full
            # User object - which a bare fetch_member() result commonly
            # isn't. That's a resolution gap, not evidence the user has no
            # name, so fall back to fetching the User object directly (whose
            # .name is always populated) rather than abandoning local-user
            # masquerade and reverting to the remote identity.
            try:
                user = await self._client.fetch_user(user_id)
            except Exception as exc:
                logger.warning(
                    "[stoat:%s] local user masquerade failed: couldn't resolve linked user %s to a "
                    "server member or a global user: %s",
                    self.connector_id,
                    user_id,
                    exc,
                )
                return None
            name = _display_name(user)
        if not name:
            logger.warning(
                "[stoat:%s] local user masquerade failed: user %s resolved but has no usable display name",
                self.connector_id,
                user_id,
            )
            return None
        # Same fetch_user() fallback for the avatar as for the name above -
        # a Member's own internal_avatar property has the same cache-miss
        # gap, so lean on the explicit override where the member carried
        # one, otherwise resolve from whichever of member/user we actually
        # fetched a usable name from (member's default-avatar fallback only
        # applies when the member itself supplied the name and had no
        # override).
        if member_avatar_override is not None:
            avatar_url = member_avatar_override.url()
        elif user is not None:
            avatar_url = _avatar_url(user)
        else:
            avatar_url = _avatar_url(member)
        logger.debug(
            "[stoat:%s] resolved local user masquerade identity for %s: name=%r avatar_url=%r",
            self.connector_id,
            user_id,
            name,
            avatar_url,
        )
        return name, avatar_url
