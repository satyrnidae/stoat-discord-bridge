from __future__ import annotations

from tests.stoat_service.conftest import FakeBotWhitelistManager, _make_ctx, _make_sender


# ---------------------------------------------------------------- _whitelist / _whitelisted


async def test_whitelist_defaults_to_add_and_the_local_connector():
    manager = FakeBotWhitelistManager()
    sender = _make_sender(bot_whitelist=manager)
    ctx = _make_ctx()

    await sender._whitelist(ctx, "add", "local", "bot1")

    assert manager.whitelist_bot_calls == [
        {"target_connector": "stoat", "bot_ref": "bot1", "added_by": "admin-1"}
    ]
    assert ctx.channel.sent[0]["content"] == "Whitelisted bot 'bot1' on Stoat."


async def test_whitelist_remove_action_targets_an_explicit_service():
    manager = FakeBotWhitelistManager()
    sender = _make_sender(bot_whitelist=manager)
    ctx = _make_ctx()

    await sender._whitelist(ctx, "remove", "discord", "bot1")

    assert manager.remove_bot_calls == [{"target_connector": "discord", "bot_ref": "bot1"}]
    assert ctx.channel.sent[0]["content"] == "Removed bot 'bot1' from the Stoat whitelist."


async def test_whitelist_without_a_configured_bot_whitelist():
    sender = _make_sender(bot_whitelist=None)
    ctx = _make_ctx()

    await sender._whitelist(ctx, "add", "local", "bot1")

    assert ctx.channel.sent[0]["content"] == "Bot whitelisting isn't configured."


async def test_whitelist_needs_admin_permission():
    manager = FakeBotWhitelistManager()
    sender = _make_sender(bot_whitelist=manager)
    ctx = _make_ctx(manage_server=False)

    await sender._whitelist(ctx, "add", "local", "bot1")

    assert ctx.channel.sent[0]["content"] == "You need the Manage Server permission to do that."
    assert manager.whitelist_bot_calls == []


async def test_whitelisted_defaults_to_the_local_connector():
    manager = FakeBotWhitelistManager()
    sender = _make_sender(bot_whitelist=manager)
    ctx = _make_ctx()

    await sender._whitelisted(ctx, "local")

    assert manager.list_whitelisted_bots_calls == [{"target_connector": "stoat"}]
    assert ctx.channel.sent[0]["content"] == "bot1 (bot1)"


async def test_whitelisted_needs_no_admin_permission():
    manager = FakeBotWhitelistManager()
    sender = _make_sender(bot_whitelist=manager)
    ctx = _make_ctx(manage_server=False)

    await sender._whitelisted(ctx, "local")  # must not be rejected

    assert manager.list_whitelisted_bots_calls


async def test_whitelisted_without_a_configured_bot_whitelist():
    sender = _make_sender(bot_whitelist=None)
    ctx = _make_ctx()

    await sender._whitelisted(ctx, "local")

    assert ctx.channel.sent[0]["content"] == "Bot whitelisting isn't configured."
