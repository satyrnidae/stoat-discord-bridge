"""Covers _normalize_channel_id and its use in the /link-channel and
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

from stoat_discord_bridge.services.discord_service import DiscordSenderService, _normalize_channel_id
from stoat_discord_bridge.status import HealthTracker
from tests.discord_service.conftest import FakeInteraction, FakeLinker, _discord_config, _make_sender, _noop


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


async def test_mirror_channel_refuses_the_invoking_channel_when_the_bot_cant_see_it():
    linker = FakeLinker()
    sender = _make_sender(linker)
    interaction = FakeInteraction(channel_id=555, channel_name="__hidden__", app_can_view=False)

    await sender._handle_mirror_channel(interaction, "stoat", None)

    assert linker.mirror_channel_calls == []
    assert linker.mirror_channel_all_calls == []
    assert interaction.deferred is False  # refused before the up-front defer
    assert interaction.sent and "can't see this channel" in interaction.sent[0]


async def test_mirror_channel_defers_before_the_slow_linker_call_and_replies_via_followup():
    # Regression for #34: creating channels+webhooks on the target outruns
    # Discord's 3s interaction deadline, so the handler must defer up front
    # and answer via followup rather than interaction.response.send_message.
    linker = FakeLinker()
    sender = _make_sender(linker)
    interaction = FakeInteraction()

    await sender._handle_mirror_channel(interaction, "stoat", None)

    assert interaction.deferred is True
    assert interaction.sent == ["ok"]


async def test_mirror_channel_link_error_is_reported_via_followup_after_defer():
    from stoat_discord_bridge.admin_commands import LinkError

    class _Boom(FakeLinker):
        async def mirror_channel(self, **kwargs):
            raise LinkError("nope")

    sender = _make_sender(_Boom())
    interaction = FakeInteraction()

    await sender._handle_mirror_channel(interaction, "stoat", None)

    assert interaction.deferred is True
    assert interaction.sent == ["nope"]


async def test_mirror_channel_from_strips_a_pasted_mention_and_routes_to_the_linker():
    linker = FakeLinker()
    sender = _make_sender(linker)
    interaction = FakeInteraction()

    await sender._handle_mirror_channel_from(interaction, "stoat", "<#814279082606592020>")

    assert linker.mirror_channel_from_calls == [
        {
            "local_connector": "discord",
            "source": "stoat",
            "source_id": "814279082606592020",
            "new_name": None,
            "local_category": None,
        }
    ]
    assert interaction.deferred is True
    assert interaction.sent == ["mirrored from ok"]


async def test_mirror_channel_from_forwards_a_new_name():
    linker = FakeLinker()
    sender = _make_sender(linker)
    interaction = FakeInteraction()

    await sender._handle_mirror_channel_from(interaction, "stoat", "s1", "lobby")

    assert linker.mirror_channel_from_calls == [
        {
            "local_connector": "discord",
            "source": "stoat",
            "source_id": "s1",
            "new_name": "lobby",
            "local_category": None,
        }
    ]


async def test_mirror_channel_forwards_the_destination_category(monkeypatch):
    linker = FakeLinker()
    sender = _make_sender(linker)

    async def fake_get_channel_name(_channel_id: str):
        return "general"

    monkeypatch.setattr(sender, "get_channel_name", fake_get_channel_name)
    interaction = FakeInteraction()

    await sender._handle_mirror_channel(interaction, "stoat", "c1", None, "Announcements")

    assert linker.mirror_channel_calls[0]["destination_category"] == "Announcements"


async def test_mirror_channel_rejects_a_category_when_mirroring_to_all():
    linker = FakeLinker()
    sender = _make_sender(linker)
    interaction = FakeInteraction()

    await sender._handle_mirror_channel(interaction, "all", "c1", None, "Announcements")

    assert linker.mirror_channel_calls == []
    assert linker.mirror_channel_all_calls == []
    assert interaction.sent and "single connector" in interaction.sent[0]


async def test_mirror_channel_from_forwards_the_local_category():
    linker = FakeLinker()
    sender = _make_sender(linker)
    interaction = FakeInteraction()

    await sender._handle_mirror_channel_from(interaction, "stoat", "s1", None, "Team Beta")

    assert linker.mirror_channel_from_calls[0]["local_category"] == "Team Beta"


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


# ---------------------------------------------------------------- _handle_link_user


async def test_link_user_uses_the_picked_members_id_not_free_text():
    # local_user is a discord.Member (a real search/pick from Discord's own
    # UI), not a typed string - this is the fix for a real bug: a plain
    # string option let someone type "@shrinerh" instead of the actual
    # snowflake, which silently linked the wrong (nonexistent) id and broke
    # mention rewriting in both directions.
    user_linker = FakeLinker()
    sender = _make_sender(FakeLinker(), user_linker=user_linker)
    interaction = FakeInteraction()
    member = SimpleNamespace(id=216591124222050304)

    await sender._handle_link_user(interaction, "stoat", "01KH7TH31EBY08FTQ7YC2RC4DQ", member)

    assert user_linker.link_user_calls == [
        {
            "local_connector": "discord",
            "local_user_id": "216591124222050304",
            "source": "stoat",
            "source_user_id": "01KH7TH31EBY08FTQ7YC2RC4DQ",
        }
    ]
    assert interaction.sent == ["user linked ok"]


async def test_link_user_without_a_configured_user_linker():
    sender = _make_sender(FakeLinker(), user_linker=None)
    interaction = FakeInteraction()

    await sender._handle_link_user(interaction, "stoat", "01KH", SimpleNamespace(id=111))

    assert interaction.sent == ["User linking isn't configured."]


# ---------------------------------------------------------------- _handle_linked_users


async def test_linked_users_with_a_member_shows_only_their_link():
    user_linker = FakeLinker()
    sender = _make_sender(FakeLinker(), user_linker=user_linker)
    interaction = FakeInteraction()
    member = SimpleNamespace(id=216591124222050304)

    await sender._handle_linked_users(interaction, member)

    assert user_linker.list_linked_users_calls == [
        {"local_connector": "discord", "local_user_id": "216591124222050304"}
    ]
    assert interaction.sent == ["Linked users:\nDiscord: ShrinerH (216591124222050304) ↔ Stoat: shriner (01KH)"]


async def test_linked_users_with_no_member_lists_everything():
    user_linker = FakeLinker()
    sender = _make_sender(FakeLinker(), user_linker=user_linker)
    interaction = FakeInteraction()

    await sender._handle_linked_users(interaction, None)

    assert user_linker.list_linked_users_calls == [{}]


async def test_linked_users_without_a_configured_user_linker():
    sender = _make_sender(FakeLinker(), user_linker=None)
    interaction = FakeInteraction()

    await sender._handle_linked_users(interaction, None)

    assert interaction.sent == ["User linking isn't configured."]


# ---------------------------------------------------------------- _handle_unlink_channel


async def test_unlink_channel_defaults_destination_to_none():
    linker = FakeLinker()
    sender = _make_sender(linker)
    interaction = FakeInteraction(channel_id=999)

    await sender._handle_unlink_channel(interaction, None, None)

    assert linker.unlink_channel_calls == [{"local_connector": "discord", "local_channel_id": "999", "destination": None}]
    assert interaction.sent == ["unlinked ok"]


async def test_unlink_channel_with_a_specific_destination():
    linker = FakeLinker()
    sender = _make_sender(linker)
    interaction = FakeInteraction(channel_id=999)

    await sender._handle_unlink_channel(interaction, "stoat", None)

    assert linker.unlink_channel_calls == [{"local_connector": "discord", "local_channel_id": "999", "destination": "stoat"}]


async def test_unlink_channel_with_a_specific_local_channel_id():
    linker = FakeLinker()
    sender = _make_sender(linker)
    interaction = FakeInteraction(channel_id=999)

    await sender._handle_unlink_channel(interaction, "stoat", "555")

    assert linker.unlink_channel_calls == [{"local_connector": "discord", "local_channel_id": "555", "destination": "stoat"}]


async def test_unlink_channel_without_a_configured_linker():
    sender = _make_sender(None)
    interaction = FakeInteraction()

    await sender._handle_unlink_channel(interaction, None, None)

    assert interaction.sent == ["Linking isn't configured."]


# ---------------------------------------------------------------- _handle_unlink_user


async def test_unlink_user_defaults_to_the_invoking_member():
    user_linker = FakeLinker()
    sender = _make_sender(FakeLinker(), user_linker=user_linker)
    interaction = FakeInteraction(user_id=111)

    await sender._handle_unlink_user(interaction, None, None)

    assert user_linker.unlink_user_calls == [{"local_connector": "discord", "local_user_id": "111", "destination": None}]
    assert interaction.sent == ["user unlinked ok"]


async def test_unlink_user_with_a_specific_destination_and_target():
    user_linker = FakeLinker()
    sender = _make_sender(FakeLinker(), user_linker=user_linker)
    interaction = FakeInteraction(user_id=111)
    member = SimpleNamespace(id=222)

    await sender._handle_unlink_user(interaction, "stoat", member)

    assert user_linker.unlink_user_calls == [{"local_connector": "discord", "local_user_id": "222", "destination": "stoat"}]


async def test_unlink_user_without_a_configured_user_linker():
    sender = _make_sender(FakeLinker(), user_linker=None)
    interaction = FakeInteraction()

    await sender._handle_unlink_user(interaction, None, None)

    assert interaction.sent == ["User linking isn't configured."]


