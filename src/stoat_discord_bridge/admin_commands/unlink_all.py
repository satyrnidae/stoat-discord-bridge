"""`/unlink all <service|all>` (issue #181): every kind's bulk `local_id: all`
unlink, run in one command against the same `destination`."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from stoat_discord_bridge.admin_commands.common import LinkError, NothingLinkedError, _is_all_token

if TYPE_CHECKING:
    from stoat_discord_bridge.admin_commands.common import ConnectorInfo

logger = logging.getLogger(__name__)


async def unlink_all(
    *,
    local_connector: str,
    destination: str | None,
    connectors: dict[str, ConnectorInfo],
    channel_linker: Any = None,
    category_linker: Any = None,
    role_linker: Any = None,
    emote_linker: Any = None,
    user_linker: Any = None,
) -> str:
    """Run each configured linker's `all` unlink for `local_connector` and
    join the results into one reply, one section per kind. A linker that's
    None (not configured on this connector) or has nothing to unlink is
    skipped; one that fails is reported in its own section without
    aborting the rest. `destination` must be given explicitly, the same as
    the per-kind bulk form."""
    if destination is None:
        raise LinkError("'all' needs an explicit service - name a connector, or 'all' to dissolve every group.")
    # (heading, linker, its unlink method, that method's local-id keyword)
    kinds = [
        ("Channels", channel_linker, "unlink_channel", "local_channel_id"),
        ("Categories", category_linker, "unlink_category", "local_category"),
        ("Roles", role_linker, "unlink_role", "local_role"),
        ("Emotes", emote_linker, "unlink_emote", "local_emote"),
        ("Users", user_linker, "unlink_user", "local_user_id"),
    ]
    sections: list[str] = []
    for heading, linker, method, id_kwarg in kinds:
        if linker is None:
            continue
        try:
            result = await getattr(linker, method)(
                local_connector=local_connector, destination=destination, **{id_kwarg: "all"}
            )
        except NothingLinkedError:
            continue
        except Exception as exc:  # report per kind, don't abort the rest
            if not isinstance(exc, LinkError):
                logger.exception("/unlink all failed for %s on %s", heading.lower(), local_connector)
            result = f"failed - {exc}"
        sections.append(f"{heading}:\n{result}")

    if not sections:

        def label(connector_id: str) -> str:
            return connectors[connector_id].label if connector_id in connectors else connector_id

        target = "anything" if _is_all_token(destination) else label(destination)
        raise NothingLinkedError(f"nothing on {label(local_connector)} is linked to {target}.")
    return "\n\n".join(sections)
