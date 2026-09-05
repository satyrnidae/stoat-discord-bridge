from __future__ import annotations

from types import SimpleNamespace

from stoat_discord_bridge.admin_commands import ConnectorInfo
from stoat_discord_bridge.services.discord_service import DiscordSenderService, _entity_autocomplete_choices
from stoat_discord_bridge.status import HealthTracker
from tests.discord_service.conftest import (
    FakeInteraction,
    FakeLinker,
    _autocomplete_callback,
    _discord_config,
    _make_sender,
    _noop,
)


# ------------------------------------------------ _entity_autocomplete_choices (external_id / local_id options)


def _entities():
    return [("111", "Admins"), ("222", "Moderators"), ("333", "Members")]


def test_entity_autocomplete_choices_lists_everything_for_an_empty_query():
    choices = _entity_autocomplete_choices("", _entities())
    assert [(c.name, c.value) for c in choices] == [
        ("Admins (111)", "111"),
        ("Moderators (222)", "222"),
        ("Members (333)", "333"),
    ]


def test_entity_autocomplete_choices_filters_by_name_substring_case_insensitively():
    choices = _entity_autocomplete_choices("mod", _entities())
    # the typed text is offered back first (issue #80), then the matches
    assert [c.value for c in choices] == ["mod", "222"]


def test_entity_autocomplete_choices_filters_by_id_substring():
    choices = _entity_autocomplete_choices("33", _entities())
    assert [c.value for c in choices] == ["33", "333"]


def test_entity_autocomplete_choices_offers_the_typed_text_when_nothing_matches():
    choices = _entity_autocomplete_choices("Wardens", _entities())
    assert [(c.name, c.value) for c in choices] == [('Use "Wardens"', "Wardens")]


def test_entity_autocomplete_choices_offers_the_typed_text_with_no_entities_at_all():
    choices = _entity_autocomplete_choices("123456789", [])
    assert [(c.name, c.value) for c in choices] == [('Use "123456789"', "123456789")]


def test_entity_autocomplete_choices_does_not_double_up_on_an_exact_id_match():
    choices = _entity_autocomplete_choices("222", _entities())
    assert [c.value for c in choices] == ["222"]


def test_entity_autocomplete_choices_does_not_double_up_on_an_exact_name_match():
    # typing a listed entity's exact name must not add a second `Use "..."`
    # choice carrying the name where the id is already on offer
    choices = _entity_autocomplete_choices("moderators", _entities())
    assert [(c.name, c.value) for c in choices] == [("Moderators (222)", "222")]


def test_entity_autocomplete_choices_ignores_a_blank_query_for_the_passthrough():
    assert _entity_autocomplete_choices("   ", []) == []


def test_entity_autocomplete_choices_uses_the_bare_id_when_there_is_no_name():
    choices = _entity_autocomplete_choices("", [("111", "")])
    assert [(c.name, c.value) for c in choices] == [("111", "111")]


def test_entity_autocomplete_choices_caps_at_25():
    many = [(str(i), f"Role {i}") for i in range(40)]
    assert len(_entity_autocomplete_choices("Role", many)) == 25


def test_entity_autocomplete_choices_clips_an_overlong_label_to_100_chars():
    [choice] = _entity_autocomplete_choices("", [("1", "x" * 200)])
    assert len(choice.name) == 100


def _connectors_with_entity_lists():
    async def discord_roles():
        return [("d1", "Local Admins")]

    async def stoat_roles():
        return [("s1", "Remote Admins"), ("s2", "Remote Mods")]

    return {
        "discord": ConnectorInfo(id="discord", label="Discord", list_roles=discord_roles),
        "stoat-public": ConnectorInfo(id="stoat-public", label="Stoat (public)", list_roles=stoat_roles),
    }


async def test_link_role_external_id_autocomplete_reads_the_connector_named_by_service():
    sender = _make_sender(FakeLinker(), role_linker=FakeLinker(_connectors_with_entity_lists()))
    callback = _autocomplete_callback(sender, "link role", "external_id")

    choices = await callback(FakeInteraction(namespace=SimpleNamespace(service="stoat-public")), "")

    assert [c.value for c in choices] == ["s1", "s2"]


async def test_link_role_external_id_autocomplete_filters_by_current():
    sender = _make_sender(FakeLinker(), role_linker=FakeLinker(_connectors_with_entity_lists()))
    callback = _autocomplete_callback(sender, "link role", "external_id")

    choices = await callback(FakeInteraction(namespace=SimpleNamespace(service="stoat-public")), "mod")

    assert [c.value for c in choices] == ["mod", "s2"]


async def test_link_role_external_id_autocomplete_is_empty_until_service_is_picked():
    sender = _make_sender(FakeLinker(), role_linker=FakeLinker(_connectors_with_entity_lists()))
    callback = _autocomplete_callback(sender, "link role", "external_id")

    assert await callback(FakeInteraction(), "") == []


async def test_link_role_local_id_autocomplete_always_reads_this_discord_connector():
    sender = _make_sender(FakeLinker(), role_linker=FakeLinker(_connectors_with_entity_lists()))
    callback = _autocomplete_callback(sender, "link role", "local_id")

    # the `service` namespace value is irrelevant for a local id
    choices = await callback(FakeInteraction(namespace=SimpleNamespace(service="stoat-public")), "")

    assert [c.value for c in choices] == ["d1"]


async def test_entity_autocomplete_handles_no_configured_linker():
    sender = DiscordSenderService(
        _discord_config(), on_message=_noop, health=HealthTracker({"discord": "Discord"}), role_linker=None
    )
    callback = _autocomplete_callback(sender, "link role", "external_id")

    assert await callback(FakeInteraction(namespace=SimpleNamespace(service="stoat-public")), "") == []


async def test_entity_autocomplete_swallows_a_raising_list_hook():
    async def boom():
        raise RuntimeError("gateway down")

    connectors = {"stoat-public": ConnectorInfo(id="stoat-public", label="Stoat", list_roles=boom)}
    sender = _make_sender(FakeLinker(), role_linker=FakeLinker(connectors))
    callback = _autocomplete_callback(sender, "link role", "external_id")

    assert await callback(FakeInteraction(namespace=SimpleNamespace(service="stoat-public")), "") == []


async def test_entity_autocomplete_is_empty_when_the_connector_has_no_list_hook():
    connectors = {"stoat-public": ConnectorInfo(id="stoat-public", label="Stoat")}  # list_roles unset (e.g. IRC)
    sender = _make_sender(FakeLinker(), role_linker=FakeLinker(connectors))
    callback = _autocomplete_callback(sender, "link role", "external_id")

    assert await callback(FakeInteraction(namespace=SimpleNamespace(service="stoat-public")), "") == []


async def test_entity_autocomplete_still_offers_typed_text_when_a_hook_is_missing_or_raises():
    # issue #80: a missing hook (IRC), a raising hook, or an un-picked service
    # must not leave a typed id/name unselectable - it's offered back verbatim.
    async def boom():
        raise RuntimeError("gateway down")

    no_hook = {"irc": ConnectorInfo(id="irc", label="IRC")}
    raising = {"stoat-public": ConnectorInfo(id="stoat-public", label="Stoat", list_roles=boom)}

    for connectors, service in ((no_hook, "irc"), (raising, "stoat-public"), (raising, None)):
        sender = _make_sender(FakeLinker(), role_linker=FakeLinker(connectors))
        callback = _autocomplete_callback(sender, "link role", "external_id")
        choices = await callback(FakeInteraction(namespace=SimpleNamespace(service=service)), "Wardens")
        assert [(c.name, c.value) for c in choices] == [('Use "Wardens"', "Wardens")]


async def test_link_channel_external_id_autocomplete_reads_list_channels():
    async def stoat_channels():
        return [("c1", "general"), ("c2", "off-topic")]

    connectors = {"stoat-public": ConnectorInfo(id="stoat-public", label="Stoat", list_channels=stoat_channels)}
    sender = _make_sender(FakeLinker(connectors))
    callback = _autocomplete_callback(sender, "link channel", "external_id")

    choices = await callback(FakeInteraction(namespace=SimpleNamespace(service="stoat-public")), "off")

    assert [c.value for c in choices] == ["off", "c2"]


async def test_link_user_external_id_autocomplete_reads_the_user_linkers_list_users():
    async def stoat_users():
        return [("u1", "corvid"), ("u2", "jay")]

    connectors = {"stoat-public": ConnectorInfo(id="stoat-public", label="Stoat", list_users=stoat_users)}
    sender = _make_sender(FakeLinker(), user_linker=FakeLinker(connectors))
    callback = _autocomplete_callback(sender, "link user", "external_id")

    choices = await callback(FakeInteraction(namespace=SimpleNamespace(service="stoat-public")), "")

    assert [c.value for c in choices] == ["u1", "u2"]


