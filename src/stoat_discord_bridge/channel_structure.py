"""Platform-neutral snapshot of a guild's channel/category layout, used by
the Stoat `/mirror-channels` admin command to recreate Discord's structure
on a Stoat server.

Stoat has no forum-channel equivalent, so each Discord forum channel is
mirrored as its own group (a Stoat category) named after the forum, holding
one channel per currently active post in it.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Stoat category/channel names are capped at 32 characters.
_NAME_LIMIT = 32


@dataclass(frozen=True)
class ChannelSpec:
    name: str
    source_channel_id: str  # native channel/thread id on the source connector, for auto-linking


@dataclass(frozen=True)
class GroupSpec:
    """A Discord category, or a forum channel mirrored as a group of its posts."""

    name: str
    channels: list[ChannelSpec] = field(default_factory=list)


@dataclass(frozen=True)
class GuildStructure:
    groups: list[GroupSpec] = field(default_factory=list)
    ungrouped_channels: list[ChannelSpec] = field(default_factory=list)


def clip_name(name: str) -> str:
    return name.strip()[:_NAME_LIMIT]
