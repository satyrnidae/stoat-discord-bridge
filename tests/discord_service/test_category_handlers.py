from __future__ import annotations

from types import SimpleNamespace

from tests.discord_service.conftest import FakeCategoryLinker, FakeInteraction, FakeLinker, _make_sender


# ---------------------------------------------------------------- _handle_linked_categories


async def test_linked_categories_reports_the_invoking_categorys_id():
    category_linker = FakeCategoryLinker()
    sender = _make_sender(FakeLinker(), category_linker=category_linker)
    interaction = FakeInteraction(category=SimpleNamespace(id=777, name="Team"))

    await sender._handle_linked_categories(interaction)

    assert category_linker.list_linked_categories_calls == [
        {"local_connector": "discord", "local_category_id": "777", "local_category": None}
    ]
    assert interaction.sent == ["Linked categories:\nDiscord: Team (999) (this Category)"]


async def test_linked_categories_without_a_configured_category_linker():
    sender = _make_sender(FakeLinker(), category_linker=None)
    interaction = FakeInteraction(category=SimpleNamespace(id=777, name="Team"))

    await sender._handle_linked_categories(interaction)

    assert interaction.sent == ["Category linking isn't configured."]


async def test_linked_categories_when_invoking_channel_has_no_category():
    category_linker = FakeCategoryLinker()
    sender = _make_sender(FakeLinker(), category_linker=category_linker)
    interaction = FakeInteraction(category=None)

    await sender._handle_linked_categories(interaction)

    assert interaction.sent == ["This channel isn't inside a Category."]
    assert category_linker.list_linked_categories_calls == []


# ---------------------------------------------------------------- _handle_link_category


async def test_link_category_uses_the_invoking_channels_category_id_and_name():
    category_linker = FakeCategoryLinker()
    sender = _make_sender(FakeLinker(), category_linker=category_linker)
    interaction = FakeInteraction(category=SimpleNamespace(id=777, name="Team"))

    await sender._handle_link_category(interaction, "stoat", "s-cat", None)

    assert category_linker.link_category_calls == [
        {
            "local_connector": "discord",
            "local_category_id": "777",
            "local_category_name": "Team",
            "source": "stoat",
            "source_id": "s-cat",
            "destination_id": None,
        }
    ]
    assert interaction.sent == ["category linked ok"]


async def test_link_category_normalizes_a_pasted_mention_in_source_and_destination_id():
    category_linker = FakeCategoryLinker()
    sender = _make_sender(FakeLinker(), category_linker=category_linker)
    interaction = FakeInteraction(category=SimpleNamespace(id=777, name="Team"))

    await sender._handle_link_category(interaction, "discord", "<#111>", "<#222>")

    call = category_linker.link_category_calls[0]
    assert call["source_id"] == "111"
    assert call["destination_id"] == "222"


async def test_link_category_without_a_configured_category_linker():
    sender = _make_sender(FakeLinker(), category_linker=None)
    interaction = FakeInteraction(category=SimpleNamespace(id=777, name="Team"))

    await sender._handle_link_category(interaction, "stoat", "s-cat", None)

    assert interaction.sent == ["Category linking isn't configured."]


async def test_link_category_when_invoking_channel_has_no_category():
    category_linker = FakeCategoryLinker()
    sender = _make_sender(FakeLinker(), category_linker=category_linker)
    interaction = FakeInteraction(category=None)

    await sender._handle_link_category(interaction, "stoat", "s-cat", None)

    assert interaction.sent == ["This channel isn't inside a Category."]
    assert category_linker.link_category_calls == []


async def test_link_category_reports_a_link_error_instead_of_raising():
    class RejectingCategoryLinker(FakeCategoryLinker):
        async def link_category(self, **kwargs):
            from stoat_discord_bridge.admin_commands import LinkError

            raise LinkError("that Category is used for thread mirroring and can't be linked")

    sender = _make_sender(FakeLinker(), category_linker=RejectingCategoryLinker())
    interaction = FakeInteraction(category=SimpleNamespace(id=777, name="Threads"))

    await sender._handle_link_category(interaction, "stoat", "s-cat", None)

    assert interaction.sent == ["that Category is used for thread mirroring and can't be linked"]


# ---------------------------------------------------------------- _handle_unlink_category


async def test_unlink_category_defaults_destination_to_none():
    category_linker = FakeCategoryLinker()
    sender = _make_sender(FakeLinker(), category_linker=category_linker)
    interaction = FakeInteraction(category=SimpleNamespace(id=777, name="Team"))

    await sender._handle_unlink_category(interaction, None)

    assert category_linker.unlink_category_calls == [
        {"local_connector": "discord", "local_category_id": "777", "local_category": None, "destination": None}
    ]
    assert interaction.sent == ["category unlinked ok"]


async def test_unlink_category_with_a_specific_destination():
    category_linker = FakeCategoryLinker()
    sender = _make_sender(FakeLinker(), category_linker=category_linker)
    interaction = FakeInteraction(category=SimpleNamespace(id=777, name="Team"))

    await sender._handle_unlink_category(interaction, None, "stoat")

    assert category_linker.unlink_category_calls == [
        {"local_connector": "discord", "local_category_id": "777", "local_category": None, "destination": "stoat"}
    ]


async def test_unlink_category_without_a_configured_category_linker():
    sender = _make_sender(FakeLinker(), category_linker=None)
    interaction = FakeInteraction(category=SimpleNamespace(id=777, name="Team"))

    await sender._handle_unlink_category(interaction, None)

    assert interaction.sent == ["Category linking isn't configured."]


async def test_unlink_category_when_invoking_channel_has_no_category():
    category_linker = FakeCategoryLinker()
    sender = _make_sender(FakeLinker(), category_linker=category_linker)
    interaction = FakeInteraction(category=None)

    await sender._handle_unlink_category(interaction, None)

    assert interaction.sent == ["This channel isn't inside a Category."]
    assert category_linker.unlink_category_calls == []


async def test_unlink_category_reports_a_link_error_instead_of_raising():
    class RejectingCategoryLinker(FakeCategoryLinker):
        async def unlink_category(self, **kwargs):
            from stoat_discord_bridge.admin_commands import LinkError

            raise LinkError("this Category isn't linked")

    sender = _make_sender(FakeLinker(), category_linker=RejectingCategoryLinker())
    interaction = FakeInteraction(category=SimpleNamespace(id=777, name="Team"))

    await sender._handle_unlink_category(interaction, None)

    assert interaction.sent == ["this Category isn't linked"]


# ---------------------------------------------------------------- _handle_mirror_category


async def test_mirror_category_all_dispatches_to_mirror_category_all():
    category_linker = FakeCategoryLinker()
    sender = _make_sender(FakeLinker(), category_linker=category_linker)
    interaction = FakeInteraction(category=SimpleNamespace(id=777, name="Team"))

    await sender._handle_mirror_category(interaction, None, None)

    assert category_linker.mirror_category_all_calls == [
        {"local_connector": "discord", "local_category_id": "777", "local_category": None}
    ]
    assert interaction.sent == ["mirrored all ok"]


async def test_mirror_category_to_a_named_local_category_and_one_destination():
    category_linker = FakeCategoryLinker()
    sender = _make_sender(FakeLinker(), category_linker=category_linker)
    interaction = FakeInteraction(category=None)

    await sender._handle_mirror_category(interaction, "Team Chat", "stoat")

    assert category_linker.mirror_category_calls == [
        {
            "local_connector": "discord",
            "local_category_id": None,
            "local_category": "Team Chat",
            "destination": "stoat",
            "new_name": None,
        }
    ]
    assert interaction.sent == ["mirrored ok"]


async def test_mirror_category_without_a_category_or_token_errors():
    category_linker = FakeCategoryLinker()
    sender = _make_sender(FakeLinker(), category_linker=category_linker)
    interaction = FakeInteraction(category=None)

    await sender._handle_mirror_category(interaction, None, None)

    assert interaction.sent == ["This channel isn't inside a Category."]
    assert category_linker.mirror_category_all_calls == []


async def test_mirror_category_from_routes_to_the_category_linker():
    category_linker = FakeCategoryLinker()
    sender = _make_sender(FakeLinker(), category_linker=category_linker)
    interaction = FakeInteraction()

    await sender._handle_mirror_category_from(interaction, "stoat", "s-cat")

    assert category_linker.mirror_category_from_calls == [
        {"local_connector": "discord", "source": "stoat", "source_id": "s-cat", "new_name": None}
    ]
    assert interaction.deferred is True
    assert interaction.sent == ["mirrored from ok"]


# ---------------------------------------------------------------- category-name hooks


async def test_resolve_category_id_by_name_matches_by_name_and_passes_ids_through(monkeypatch):
    sender = _make_sender(FakeLinker())
    guild = SimpleNamespace(
        categories=[SimpleNamespace(id=10, name="Team Chat"), SimpleNamespace(id=20, name="Ops")]
    )
    monkeypatch.setattr(sender, "_guild_or_none", lambda: guild)

    assert await sender.resolve_category_id_by_name("team chat") == "10"
    assert await sender.resolve_category_id_by_name("10") == "10"
    assert await sender.resolve_category_id_by_name("missing") is None


# ---------------------------------------------------------------- can_view_channel


def _guild_with_channel(*, channel_id: int, can_view: bool, bot_member: object | None = "me"):
    perms = SimpleNamespace(view_channel=can_view)
    channel = SimpleNamespace(id=channel_id, permissions_for=lambda _m: perms)
    return SimpleNamespace(
        get_channel_or_thread=lambda cid: channel if cid == channel_id else None,
        me=bot_member,
    )


async def test_can_view_channel_reflects_the_bot_permission(monkeypatch):
    sender = _make_sender(FakeLinker())
    monkeypatch.setattr(sender, "_guild_or_none", lambda: _guild_with_channel(channel_id=7, can_view=True))
    assert await sender.can_view_channel("7") is True

    monkeypatch.setattr(sender, "_guild_or_none", lambda: _guild_with_channel(channel_id=7, can_view=False))
    assert await sender.can_view_channel("7") is False


async def test_can_view_channel_is_none_when_the_channel_or_guild_is_unknown(monkeypatch):
    sender = _make_sender(FakeLinker())
    monkeypatch.setattr(sender, "_guild_or_none", lambda: None)
    assert await sender.can_view_channel("7") is None

    monkeypatch.setattr(sender, "_guild_or_none", lambda: _guild_with_channel(channel_id=7, can_view=True))
    assert await sender.can_view_channel("999") is None
    assert await sender.can_view_channel("not-an-int") is None


