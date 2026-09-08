"""Shared fixtures/fakes for the DiscordSenderService test package
(`tests/discord_service/`) - constructing one only builds a discord.py
Client/CommandTree in memory (no network), the same way IrcSenderService's
constructor doesn't touch a socket, so a real instance is safe to build here.
"""

from __future__ import annotations

from types import SimpleNamespace

from stoat_discord_bridge.config import DiscordConnectorConfig
from stoat_discord_bridge.services.discord_service import DiscordSenderService
from stoat_discord_bridge.status import HealthTracker

__all__ = [
    "FakeLinker",
    "FakeCategoryLinker",
    "FakeInteraction",
    "_discord_config",
    "_noop",
    "_make_sender",
    "_autocomplete_callback",
]


def _autocomplete_callback(sender: DiscordSenderService, command_name: str, param_name: str):
    # `command_name` is either a flat command name ("link-emote") or a
    # "group sub" pair ("link channel") for an app_commands.Group subcommand.
    parts = command_name.split()
    command = sender.tree.get_command(parts[0], guild=sender._guild)
    for sub in parts[1:]:
        command = command.get_command(sub)
    return command._params[param_name].autocomplete


def _discord_config(**overrides):
    defaults = dict(id="discord", label="Discord", guild_id=123, bot_token="fake-token")
    defaults.update(overrides)
    return DiscordConnectorConfig(**defaults)


async def _noop(_message) -> None:
    pass


class FakeLinker:
    def __init__(self, connectors: dict | None = None):
        self.mirror_channel_calls: list[dict] = []
        self.mirror_channel_all_calls: list[dict] = []
        self.mirror_channel_from_calls: list[dict] = []
        self.link_channel_calls: list[dict] = []
        self.list_linked_channels_calls: list[dict] = []
        self.link_user_calls: list[dict] = []
        self.list_linked_users_calls: list[dict] = []
        self.unlink_channel_calls: list[dict] = []
        self.unlink_user_calls: list[dict] = []
        self.connectors = connectors or {}

    async def link_user(self, **kwargs):
        self.link_user_calls.append(kwargs)
        return "user linked ok"

    async def list_linked_users(self, **kwargs):
        self.list_linked_users_calls.append(kwargs)
        return "Linked users:\nDiscord: ShrinerH (216591124222050304) ↔ Stoat: shriner (01KH)"

    async def mirror_channel(self, **kwargs):
        self.mirror_channel_calls.append(kwargs)
        return "ok"

    async def mirror_channel_all(self, **kwargs):
        self.mirror_channel_all_calls.append(kwargs)
        return "ok"

    async def mirror_channel_from(self, **kwargs):
        self.mirror_channel_from_calls.append(kwargs)
        return "mirrored from ok"

    async def link_channel(self, **kwargs):
        self.link_channel_calls.append(kwargs)
        return "ok"

    async def list_linked_channels(self, **kwargs):
        self.list_linked_channels_calls.append(kwargs)
        return "Linked channels:\nDiscord: general (999) (this channel)"

    async def unlink_channel(self, **kwargs):
        self.unlink_channel_calls.append(kwargs)
        return "unlinked ok"

    async def unlink_user(self, **kwargs):
        self.unlink_user_calls.append(kwargs)
        return "user unlinked ok"


class FakeCategoryLinker:
    def __init__(self, connectors: dict | None = None):
        self.link_category_calls: list[dict] = []
        self.list_linked_categories_calls: list[dict] = []
        self.unlink_category_calls: list[dict] = []
        self.sync_new_channel_calls: list[dict] = []
        self.mirror_category_calls: list[dict] = []
        self.mirror_category_all_calls: list[dict] = []
        self.mirror_category_from_calls: list[dict] = []
        self.connectors = connectors or {}

    async def link_category(self, **kwargs):
        self.link_category_calls.append(kwargs)
        return "category linked ok"

    async def list_linked_categories(self, **kwargs):
        self.list_linked_categories_calls.append(kwargs)
        return "Linked categories:\nDiscord: Team (999) (this Category)"

    async def unlink_category(self, **kwargs):
        self.unlink_category_calls.append(kwargs)
        return "category unlinked ok"

    async def sync_new_channel(self, **kwargs):
        self.sync_new_channel_calls.append(kwargs)

    async def mirror_category(self, **kwargs):
        self.mirror_category_calls.append(kwargs)
        return "mirrored ok"

    async def mirror_category_all(self, **kwargs):
        self.mirror_category_all_calls.append(kwargs)
        return "mirrored all ok"

    async def mirror_category_from(self, **kwargs):
        self.mirror_category_from_calls.append(kwargs)
        return "mirrored from ok"


class FakeInteraction:
    def __init__(
        self,
        channel_id: int = 999,
        channel_name: str = "current-channel",
        user_id: int = 1,
        category: SimpleNamespace | None = None,
        namespace: SimpleNamespace | None = None,
        app_can_view: bool = True,
    ):
        self.channel_id = channel_id
        self.channel = SimpleNamespace(name=channel_name, category=category)
        self.user = SimpleNamespace(id=user_id)
        # discord.py fills this from the interaction payload - the app's
        # computed permissions in the channel the command was run in.
        self.app_permissions = SimpleNamespace(view_channel=app_can_view)
        # discord.py exposes the other, already-filled option values on
        # `interaction.namespace` during an autocomplete callback - the
        # `external_id` autocomplete reads `.service` off it.
        self.namespace = namespace if namespace is not None else SimpleNamespace()
        self.sent: list[str] = []
        self.response = SimpleNamespace(send_message=self._send_message, defer=self._defer)
        self.followup = SimpleNamespace(send=self._send_message)
        self.deferred = False

    async def _send_message(self, content, ephemeral=False):
        self.sent.append(content)

    async def _defer(self, ephemeral=False, thinking=False):
        self.deferred = True


def _make_sender(
    linker: FakeLinker, *, emote_linker=None, user_linker=None, category_linker=None, role_linker=None
) -> DiscordSenderService:
    return DiscordSenderService(
        _discord_config(),
        on_message=_noop,
        health=HealthTracker({"discord": "Discord"}),
        linker=linker,
        emote_linker=emote_linker,
        user_linker=user_linker,
        category_linker=category_linker,
        role_linker=role_linker,
    )
