"""Tests for the pieces of DiscordSenderService that don't require an actual
Discord connection - constructing one only builds a discord.Client/
CommandTree in memory (no network) the same way IrcSenderService's
constructor doesn't touch a socket, so a real instance is safe to build here.

Covers _normalize_channel_id and its use in the /link-channel and
/mirror-channel handlers: a real bug where pasting a Discord channel mention
(the `<#id>` text the client inserts when a user picks a channel from the
"#" autocomplete inside a plain string slash-command option) into
/mirror-channel's `local_channel_id` caused a Stoat channel literally named
"<#814279082606592020>" to get created, because the mention text flowed
straight through as both the channel id *and*, since no id-based name
lookup was attempted, the display name handed to the other connector's
ensure_channel().
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from stoat_discord_bridge.config import DiscordConnectorConfig
from stoat_discord_bridge.services.discord_service import DiscordSenderService, _normalize_channel_id
from stoat_discord_bridge.status import HealthTracker


def _discord_config(**overrides):
    defaults = dict(id="discord", label="Discord", guild_id=123, bot_token="fake-token")
    defaults.update(overrides)
    return DiscordConnectorConfig(**defaults)


async def _noop(_message) -> None:
    pass


class FakeLinker:
    def __init__(self):
        self.mirror_channel_calls: list[dict] = []
        self.mirror_channel_all_calls: list[dict] = []
        self.link_channel_calls: list[dict] = []
        self.list_linked_channels_calls: list[dict] = []

    async def mirror_channel(self, **kwargs):
        self.mirror_channel_calls.append(kwargs)
        return "ok"

    async def mirror_channel_all(self, **kwargs):
        self.mirror_channel_all_calls.append(kwargs)
        return "ok"

    async def link_channel(self, **kwargs):
        self.link_channel_calls.append(kwargs)
        return "ok"

    async def list_linked_channels(self, **kwargs):
        self.list_linked_channels_calls.append(kwargs)
        return "Linked channels:\nDiscord: general (999) (this channel)"


class FakeInteraction:
    def __init__(self, channel_id: int = 999, channel_name: str = "current-channel"):
        self.channel_id = channel_id
        self.channel = SimpleNamespace(name=channel_name)
        self.sent: list[str] = []
        self.response = SimpleNamespace(send_message=self._send_message)

    async def _send_message(self, content, ephemeral=False):
        self.sent.append(content)


def _make_sender(linker: FakeLinker) -> DiscordSenderService:
    return DiscordSenderService(_discord_config(), on_message=_noop, health=HealthTracker({"discord": "Discord"}), linker=linker)


# ---------------------------------------------------------------- _normalize_channel_id


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("<#814279082606592020>", "814279082606592020"),
        ("814279082606592020", "814279082606592020"),
        ("general", "general"),  # a Stoat/IRC-style id must pass through untouched
        (" <#123> ", "123"),  # tolerate incidental whitespace
    ],
)
def test_normalize_channel_id(raw, expected):
    assert _normalize_channel_id(raw) == expected


# ---------------------------------------------------------------- _handle_mirror_channel


async def test_mirror_channel_strips_a_pasted_mention_and_resolves_its_real_name(monkeypatch):
    linker = FakeLinker()
    sender = _make_sender(linker)

    async def fake_get_channel_name(channel_id: str):
        assert channel_id == "814279082606592020"
        return "general"

    monkeypatch.setattr(sender, "get_channel_name", fake_get_channel_name)
    interaction = FakeInteraction()

    await sender._handle_mirror_channel(interaction, "stoat", "<#814279082606592020>")

    assert len(linker.mirror_channel_calls) == 1
    call = linker.mirror_channel_calls[0]
    assert call["local_channel_id"] == "814279082606592020"
    assert call["local_channel_name"] == "general"


async def test_mirror_channel_falls_back_to_bare_id_when_name_unresolvable(monkeypatch):
    linker = FakeLinker()
    sender = _make_sender(linker)

    async def fake_get_channel_name(_channel_id: str):
        return None  # e.g. the channel isn't in this guild's cache

    monkeypatch.setattr(sender, "get_channel_name", fake_get_channel_name)
    interaction = FakeInteraction()

    await sender._handle_mirror_channel(interaction, "all", "<#42>")

    call = linker.mirror_channel_all_calls[0]
    assert call["local_channel_id"] == "42"
    assert call["local_channel_name"] == "42"


async def test_mirror_channel_uses_invoking_channel_when_no_explicit_id_given():
    linker = FakeLinker()
    sender = _make_sender(linker)
    interaction = FakeInteraction(channel_id=555, channel_name="the-current-one")

    await sender._handle_mirror_channel(interaction, "stoat", None)

    call = linker.mirror_channel_calls[0]
    assert call["local_channel_id"] == "555"
    assert call["local_channel_name"] == "the-current-one"


# ---------------------------------------------------------------- _handle_link_channel


async def test_link_channel_normalizes_a_pasted_mention_in_destination_id():
    linker = FakeLinker()
    sender = _make_sender(linker)
    interaction = FakeInteraction()

    await sender._handle_link_channel(interaction, "stoat", "s1", "<#814279082606592020>")

    call = linker.link_channel_calls[0]
    assert call["destination_id"] == "814279082606592020"


async def test_link_channel_normalizes_a_pasted_mention_in_source_id():
    linker = FakeLinker()
    sender = _make_sender(linker)
    interaction = FakeInteraction()

    await sender._handle_link_channel(interaction, "discord", "<#111>", None)

    call = linker.link_channel_calls[0]
    assert call["source_id"] == "111"


async def test_link_channel_leaves_a_missing_destination_id_as_none():
    linker = FakeLinker()
    sender = _make_sender(linker)
    interaction = FakeInteraction()

    await sender._handle_link_channel(interaction, "stoat", "s1", None)

    call = linker.link_channel_calls[0]
    assert call["destination_id"] is None


# ---------------------------------------------------------------- _handle_linked_channels


async def test_linked_channels_reports_the_invoking_channel():
    linker = FakeLinker()
    sender = _make_sender(linker)
    interaction = FakeInteraction(channel_id=555)

    await sender._handle_linked_channels(interaction)

    assert linker.list_linked_channels_calls == [{"local_connector": "discord", "local_channel_id": "555"}]
    assert interaction.sent == ["Linked channels:\nDiscord: general (999) (this channel)"]


async def test_linked_channels_without_a_configured_linker():
    sender = DiscordSenderService(
        _discord_config(), on_message=_noop, health=HealthTracker({"discord": "Discord"}), linker=None
    )
    interaction = FakeInteraction()

    await sender._handle_linked_channels(interaction)

    assert interaction.sent == ["Linking isn't configured."]
