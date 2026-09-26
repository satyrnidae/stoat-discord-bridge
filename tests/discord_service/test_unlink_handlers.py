"""The Discord `/unlink <noun>` handlers' `all` support (issue #181): every
bulk-capable handler defers first (a fan-out can outrun Discord's 3s window,
issue #177), `/unlink user` takes a string `local_id` so `all` is typeable,
and `/unlink all` runs every configured linker at once."""

from __future__ import annotations

import pytest

from stoat_discord_bridge.admin_commands import ChannelLinker, ConnectorInfo, UserLinker
from stoat_discord_bridge.storage.channel_mappings import ChannelMappingRepository
from stoat_discord_bridge.storage.user_mappings import UserMappingRepository
from tests.discord_service.conftest import (
    FakeInteraction,
    FakeLinker,
    _autocomplete_callback,
    _make_sender,
)


class _RecordingLinker:
    """Records every `unlink_*` call and answers with a fixed summary."""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self.connectors: dict = {}

    def __getattr__(self, name):
        if not name.startswith("unlink_"):
            raise AttributeError(name)

        async def _unlink(**kwargs):
            self.calls.append((name, kwargs))
            return f"{name} ok"

        return _unlink


@pytest.mark.parametrize(
    "handler, linker_kwarg, args, expected",
    [
        ("_handle_unlink_role", "role_linker", ("all", "stoat"), ("unlink_role", {"local_role": "all"})),
        ("_handle_unlink_emote", "emote_linker", ("all", "stoat"), ("unlink_emote", {"local_emote": "all"})),
        ("_handle_unlink_category", "category_linker", ("all", "stoat"), ("unlink_category", {"local_category": "all"})),
    ],
)
async def test_bulk_capable_unlink_handlers_defer_before_the_linker_call(handler, linker_kwarg, args, expected):
    linker = _RecordingLinker()
    sender = _make_sender(FakeLinker(), **{linker_kwarg: linker})
    interaction = FakeInteraction()

    await getattr(sender, handler)(interaction, *args)

    assert interaction.deferred is True
    method, kwargs = linker.calls[0]
    assert method == expected[0]
    assert kwargs.items() >= {**expected[1], "destination": "stoat"}.items()
    assert interaction.sent == [f"{method} ok"]


# ---------------------------------------------------------------- /unlink user (string local_id)


async def test_unlink_user_accepts_all_as_local_id():
    user_linker = FakeLinker()
    sender = _make_sender(FakeLinker(), user_linker=user_linker)
    interaction = FakeInteraction(user_id=111)

    await sender._handle_unlink_user(interaction, "stoat", "all")

    assert user_linker.unlink_user_calls == [{"local_connector": "discord", "local_user_id": "all", "destination": "stoat"}]
    assert interaction.deferred is True


def test_unlink_user_local_id_is_an_autocompleted_string_option():
    sender = _make_sender(FakeLinker(), user_linker=FakeLinker())
    command = sender.tree.get_command("unlink", guild=sender._guild).get_command("user")

    assert command._params["local_id"].type.name == "string"
    assert _autocomplete_callback(sender, "unlink user", "local_id") is not None


# ---------------------------------------------------------------- /unlink all


def _real_linkers(fake_db):
    connectors = {
        "discord": ConnectorInfo(id="discord", label="Discord"),
        "stoat": ConnectorInfo(id="stoat", label="Stoat"),
    }
    return ChannelLinker(ChannelMappingRepository(fake_db), connectors), UserLinker(
        UserMappingRepository(fake_db), connectors
    )


async def test_unlink_all_defers_and_unlinks_every_configured_kind(fake_db):
    channel_linker, user_linker = _real_linkers(fake_db)
    await channel_linker.link_channel(
        local_connector="discord", local_channel_id="d1", local_channel_name="general",
        source="stoat", source_id="s1", destination_id=None,
    )
    await user_linker.link_user(local_connector="discord", local_user_id="111", source="stoat", source_user_id="s-alice")
    sender = _make_sender(channel_linker, user_linker=user_linker)
    interaction = FakeInteraction()

    await sender._handle_unlink_all(interaction, "all")

    assert interaction.deferred is True
    [reply] = interaction.sent
    assert "Channels:\nDissolved 1 bridge group(s) on Discord:" in reply
    assert "Users:\nDissolved 1 link group(s) on Discord:" in reply
    assert await ChannelMappingRepository(fake_db).get_bridge_group("discord", "d1") is None


async def test_unlink_all_relays_a_nothing_linked_error(fake_db):
    channel_linker, user_linker = _real_linkers(fake_db)
    sender = _make_sender(channel_linker, user_linker=user_linker)
    interaction = FakeInteraction()

    await sender._handle_unlink_all(interaction, "stoat")

    assert interaction.sent == ["nothing on Discord is linked to Stoat."]


async def test_unlink_all_without_a_configured_linker():
    sender = _make_sender(None)
    interaction = FakeInteraction()

    await sender._handle_unlink_all(interaction, "all")

    assert interaction.sent == ["Linking isn't configured."]


def test_unlink_all_is_registered_with_a_service_autocomplete():
    sender = _make_sender(FakeLinker())
    assert _autocomplete_callback(sender, "unlink all", "service") is not None
