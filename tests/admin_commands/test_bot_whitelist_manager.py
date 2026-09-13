import pytest

from stoat_discord_bridge.admin_commands import BotWhitelistManager, ConnectorInfo, LinkError
from stoat_discord_bridge.storage.bot_whitelist import BotWhitelistRepository
from stoat_discord_bridge.storage.user_mappings import UserMapping, UserMappingRepository


def _manager(fake_db, connectors, *, seed=frozenset()):
    return BotWhitelistManager(
        BotWhitelistRepository(fake_db), UserMappingRepository(fake_db), connectors, seed=seed
    )


# ---------------------------------------------------------------- whitelist_bot


async def test_whitelist_bot_on_an_explicit_target(fake_db, connectors):
    manager = _manager(fake_db, connectors)
    summary = await manager.whitelist_bot(target_connector="discord", bot_ref="bot1")
    assert "Whitelisted bot 'bot1' on Discord." == summary
    assert await manager.is_whitelisted("discord", "bot1") is True


async def test_whitelist_bot_unknown_connector_raises(fake_db, connectors):
    manager = _manager(fake_db, connectors)
    with pytest.raises(LinkError, match="isn't a known connector"):
        await manager.whitelist_bot(target_connector="nope", bot_ref="bot1")


async def test_whitelist_bot_already_whitelisted_raises(fake_db, connectors):
    manager = _manager(fake_db, connectors)
    await manager.whitelist_bot(target_connector="discord", bot_ref="bot1")
    with pytest.raises(LinkError, match="already whitelisted"):
        await manager.whitelist_bot(target_connector="discord", bot_ref="bot1")


async def test_whitelist_bot_refuses_the_bridges_own_bot(fake_db):
    connectors = {
        "discord": ConnectorInfo(id="discord", label="Discord", self_user_id=lambda: "bridge-bot-id"),
    }
    manager = _manager(fake_db, connectors)
    with pytest.raises(LinkError, match="bridge's own bot"):
        await manager.whitelist_bot(target_connector="discord", bot_ref="bridge-bot-id")


async def test_whitelist_bot_resolves_a_bare_name(fake_db):
    async def by_name(token):
        return {"webhookbot": "12345"}.get(token.casefold())

    async def name_of(user_id):
        return {"12345": "WebhookBot"}.get(user_id)

    connectors = {
        "discord": ConnectorInfo(
            id="discord", label="Discord", resolve_user_id_by_name=by_name, resolve_user_name=name_of
        ),
    }
    manager = _manager(fake_db, connectors)

    summary = await manager.whitelist_bot(target_connector="discord", bot_ref="WebhookBot")

    assert "Whitelisted bot 'WebhookBot' on Discord." == summary
    assert await manager.is_whitelisted("discord", "12345") is True


async def test_whitelist_bot_strips_a_pasted_discord_mention(fake_db, connectors):
    manager = _manager(fake_db, connectors)
    await manager.whitelist_bot(target_connector="discord", bot_ref="<@216591124222050304>")
    assert await manager.is_whitelisted("discord", "216591124222050304") is True


# ---------------------------------------------------------------- remove_bot


async def test_remove_bot(fake_db, connectors):
    manager = _manager(fake_db, connectors)
    await manager.whitelist_bot(target_connector="discord", bot_ref="bot1")

    summary = await manager.remove_bot(target_connector="discord", bot_ref="bot1")

    assert "Removed bot 'bot1' from the Discord whitelist." == summary
    assert await manager.is_whitelisted("discord", "bot1") is False


async def test_remove_bot_not_whitelisted_raises(fake_db, connectors):
    manager = _manager(fake_db, connectors)
    with pytest.raises(LinkError, match="wasn't whitelisted"):
        await manager.remove_bot(target_connector="discord", bot_ref="bot1")


async def test_remove_bot_refuses_a_config_pinned_entry(fake_db, connectors):
    manager = _manager(fake_db, connectors, seed=frozenset({("discord", "bot1")}))
    with pytest.raises(LinkError, match="config-pinned|pinned in config.yaml"):
        await manager.remove_bot(target_connector="discord", bot_ref="bot1")


# ---------------------------------------------------------------- is_whitelisted


async def test_is_whitelisted_true_for_a_seed_entry(fake_db, connectors):
    manager = _manager(fake_db, connectors, seed=frozenset({("discord", "seed-bot")}))
    assert await manager.is_whitelisted("discord", "seed-bot") is True


async def test_is_whitelisted_false_for_an_unknown_bot(fake_db, connectors):
    manager = _manager(fake_db, connectors)
    assert await manager.is_whitelisted("discord", "nope") is False


async def test_is_whitelisted_true_for_a_linked_identity(fake_db, connectors):
    manager = _manager(fake_db, connectors)
    await manager.whitelist_bot(target_connector="discord", bot_ref="bot1")
    user_mappings = UserMappingRepository(fake_db)
    await user_mappings.upsert(UserMapping(link_group="g1", connector_id="discord", user_id="bot1", display_name="bot1"))
    await user_mappings.upsert(
        UserMapping(link_group="g1", connector_id="stoat", user_id="stoat-bot1", display_name="stoat-bot1")
    )

    assert await manager.is_whitelisted("stoat", "stoat-bot1") is True


async def test_is_whitelisted_false_for_a_linked_identity_when_neither_side_is_whitelisted(fake_db, connectors):
    manager = _manager(fake_db, connectors)
    user_mappings = UserMappingRepository(fake_db)
    await user_mappings.upsert(UserMapping(link_group="g1", connector_id="discord", user_id="bot1", display_name="bot1"))
    await user_mappings.upsert(UserMapping(link_group="g1", connector_id="stoat", user_id="stoat-bot1", display_name="stoat-bot1"))

    assert await manager.is_whitelisted("stoat", "stoat-bot1") is False


# ---------------------------------------------------------------- list_whitelisted_bots


async def test_list_whitelisted_bots_shows_seed_and_runtime_entries(fake_db, connectors):
    manager = _manager(fake_db, connectors, seed=frozenset({("discord", "seed-bot")}))
    await manager.whitelist_bot(target_connector="discord", bot_ref="runtime-bot")

    summary = await manager.list_whitelisted_bots(target_connector="discord")

    assert "seed-bot" in summary and "(config)" in summary
    assert "runtime-bot" in summary
    seed_line = next(line for line in summary.splitlines() if "seed-bot" in line)
    runtime_line = next(line for line in summary.splitlines() if "runtime-bot" in line)
    assert "(config)" in seed_line
    assert "(config)" not in runtime_line


async def test_list_whitelisted_bots_empty(fake_db, connectors):
    manager = _manager(fake_db, connectors)
    summary = await manager.list_whitelisted_bots(target_connector="discord")
    assert summary == "No bots are whitelisted on Discord."


async def test_list_whitelisted_bots_only_shows_the_target_connectors_seed_entries(fake_db, connectors):
    manager = _manager(fake_db, connectors, seed=frozenset({("stoat", "other-bot")}))
    summary = await manager.list_whitelisted_bots(target_connector="discord")
    assert "other-bot" not in summary
