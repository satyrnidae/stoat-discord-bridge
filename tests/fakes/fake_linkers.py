"""Shared fake `ChannelLinker`/`UserLinker` doubles for the Stoat and IRC
admin-command dispatch suites (`tests/stoat_admin/`, `tests/test_irc_admin_dispatch.py`)
- both connectors' `_link_channel`/`_mirror_channel`/... handlers forward to the same
`admin_commands` linker shape, so the fakes standing in for them were previously
declared twice, identically apart from the canned `list_linked_channels` summary
text (each platform's real linker echoes back its own connector label)."""

from __future__ import annotations

from stoat_discord_bridge.admin_commands import LinkError


class FakeLinker:
    def __init__(
        self,
        *,
        raises: LinkError | None = None,
        list_linked_channels_summary: str = "Linked channels:\n(this channel)",
    ) -> None:
        self._raises = raises
        self._list_linked_channels_summary = list_linked_channels_summary
        self.link_channel_calls: list[dict] = []
        self.mirror_channel_calls: list[dict] = []
        self.mirror_channel_all_calls: list[dict] = []
        self.mirror_channel_from_calls: list[dict] = []
        self.list_linked_channels_calls: list[dict] = []
        self.unlink_channel_calls: list[dict] = []

    async def link_channel(self, **kwargs):
        self.link_channel_calls.append(kwargs)
        if self._raises is not None:
            raise self._raises
        return "linked ok"

    async def list_linked_channels(self, **kwargs):
        self.list_linked_channels_calls.append(kwargs)
        return self._list_linked_channels_summary

    async def mirror_channel(self, **kwargs):
        self.mirror_channel_calls.append(kwargs)
        if self._raises is not None:
            raise self._raises
        return "mirrored ok"

    async def mirror_channel_all(self, **kwargs):
        self.mirror_channel_all_calls.append(kwargs)
        if self._raises is not None:
            raise self._raises
        return "mirrored to all ok"

    async def mirror_channel_from(self, **kwargs):
        self.mirror_channel_from_calls.append(kwargs)
        if self._raises is not None:
            raise self._raises
        return "mirrored from ok"

    async def unlink_channel(self, **kwargs):
        self.unlink_channel_calls.append(kwargs)
        if self._raises is not None:
            raise self._raises
        return "unlinked ok"


class FakeUserLinker:
    def __init__(self, *, raises: LinkError | None = None) -> None:
        self._raises = raises
        self.calls: list[dict] = []
        self.list_linked_users_calls: list[dict] = []
        self.unlink_user_calls: list[dict] = []

    async def link_user(self, **kwargs):
        self.calls.append(kwargs)
        if self._raises is not None:
            raise self._raises
        return "user linked ok"

    async def list_linked_users(self, **kwargs):
        self.list_linked_users_calls.append(kwargs)
        return "Linked users:\nDiscord: ShrinerH (216591124222050304) ↔ Stoat: shriner (01KH)"

    async def unlink_user(self, **kwargs):
        self.unlink_user_calls.append(kwargs)
        if self._raises is not None:
            raise self._raises
        return "user unlinked ok"
