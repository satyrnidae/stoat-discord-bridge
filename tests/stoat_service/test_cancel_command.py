"""Long-running Stoat commands post a "working" placeholder with a ❌
reaction (issue #200). The invoker reacting ❌ to it cancels the running
linker call, and that reaction never reaches reaction sync."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from stoat_discord_bridge.services.long_running import CANCELED_TEXT
from stoat_discord_bridge.services.stoat_service.client import _StoatClient
from stoat_discord_bridge.services.stoat_service.linking import CANCEL_EMOJI
from tests.fakes.fake_stoat import FakeChannel
from tests.stoat_service.conftest import _make_ctx, _make_sender


class _Universal:
    """Any linker method returns "ok" at once, or blocks until canceled when
    `block` is set - enough for every handler this feature touches."""

    def __init__(self, *, block: bool = False) -> None:
        self.connectors: dict = {}
        self.block = block
        self.started = asyncio.Event()
        self.canceled = False

    def __getattr__(self, name):
        async def call(**kwargs):
            self.started.set()
            if self.block:
                try:
                    await asyncio.sleep(10)
                except asyncio.CancelledError:
                    self.canceled = True
                    raise
            return "ok"

        return call


def _sender(linker, **kwargs):
    return _make_sender(
        linker=linker, category_linker=linker, role_linker=linker, emote_linker=linker, user_linker=linker,
        self_id="bot", **kwargs,
    )


def _ctx():
    return _make_ctx(channel=FakeChannel(id="c1", name="general"))


def _react(message_id: str, *, user_id: str = "admin-1", emoji: str = CANCEL_EMOJI):
    return SimpleNamespace(channel_id="c1", message_id=message_id, user_id=user_id, emoji=emoji, message=None)


class _Owner:
    """Wraps a real sender so the client hook's reaction-sync call is visible."""

    def __init__(self, sender) -> None:
        self.sender = sender
        self.synced: list[object] = []

    def _cancel_command_by_reaction(self, event) -> bool:
        return self.sender._cancel_command_by_reaction(event)

    async def _handle_message_react(self, event, *, added: bool) -> None:
        self.synced.append(event)


async def _react_via_client(owner: _Owner, event) -> None:
    await _StoatClient.on_message_react(SimpleNamespace(_owner=owner), event)


async def test_placeholder_is_reacted_with_the_cancel_emoji():
    sender = _sender(_Universal())
    ctx = _ctx()

    await sender._transfer_history(ctx, "import", "discord", "123")

    placeholder = ctx.channel.sent[0]
    assert placeholder["reactions"] == [CANCEL_EMOJI]
    assert placeholder["unreactions"] == [CANCEL_EMOJI]
    assert placeholder["content"] == "ok"
    # finished - a late ❌ can't cancel anything any more
    assert sender._cancelable_commands == {}


async def test_invoker_reacting_cancel_cancels_the_command():
    linker = _Universal(block=True)
    sender = _sender(linker)
    ctx = _ctx()
    owner = _Owner(sender)

    run = asyncio.create_task(sender._unlink_all(ctx, "all"))
    await linker.started.wait()
    (message_id,) = sender._cancelable_commands
    await _react_via_client(owner, _react(message_id))
    await run

    assert linker.canceled
    assert owner.synced == []
    assert ctx.channel.sent[0]["content"] == CANCELED_TEXT
    assert sender._cancelable_commands == {}


async def test_someone_elses_cancel_reaction_is_ignored_but_not_relayed():
    linker = _Universal(block=True)
    sender = _sender(linker)
    ctx = _ctx()
    owner = _Owner(sender)

    run = asyncio.create_task(sender._unlink_all(ctx, "all"))
    await linker.started.wait()
    (message_id,) = sender._cancelable_commands
    await _react_via_client(owner, _react(message_id, user_id="someone-else"))
    await _react_via_client(owner, _react(message_id, user_id="bot"))

    assert not run.done()
    assert owner.synced == []
    run.cancel()
    with pytest.raises(asyncio.CancelledError):
        await run


async def test_other_reactions_fall_through_to_reaction_sync():
    linker = _Universal(block=True)
    sender = _sender(linker)
    ctx = _ctx()
    owner = _Owner(sender)

    run = asyncio.create_task(sender._unlink_all(ctx, "all"))
    await linker.started.wait()
    (message_id,) = sender._cancelable_commands
    other_emoji = _react(message_id, emoji="👍")
    unregistered = _react("not-a-placeholder")
    await _react_via_client(owner, other_emoji)
    await _react_via_client(owner, unregistered)

    assert owner.synced == [other_emoji, unregistered]
    assert not run.done()
    run.cancel()
    with pytest.raises(asyncio.CancelledError):
        await run


async def test_a_quick_command_gets_no_cancel_reaction():
    sender = _sender(_Universal())
    ctx = _ctx()

    await sender._link_channel(ctx, "discord", "123")

    assert "reactions" not in ctx.channel.sent[0]


@pytest.mark.parametrize(
    "call",
    [
        pytest.param(lambda s, c: s._mirror_channel(c, "discord", "general", with_history=True), id="mirror-history"),
        pytest.param(
            lambda s, c: s._mirror_channel_from(c, "discord", "123", with_history=True), id="mirror-from-history"
        ),
        pytest.param(lambda s, c: s._mirror_channel(c, "discord", "all"), id="mirror-channel-all"),
        pytest.param(lambda s, c: s._mirror_channel_from(c, "discord", "all"), id="mirror-channel-from-all"),
        pytest.param(lambda s, c: s._mirror_channel(c, "all", "general"), id="mirror-channel-to-all"),
        pytest.param(lambda s, c: s._transfer_history(c, "import", "discord", "123"), id="import"),
        pytest.param(lambda s, c: s._transfer_history(c, "export", "discord", "123"), id="export"),
        pytest.param(lambda s, c: s._mirror_category(c, "discord", "Team"), id="mirror-category"),
        pytest.param(lambda s, c: s._mirror_category_from(c, "discord", "123"), id="mirror-category-from"),
        pytest.param(lambda s, c: s._mirror_role(c, "discord", "all"), id="mirror-role-all"),
        pytest.param(lambda s, c: s._mirror_role_from(c, "discord", "all"), id="mirror-role-from-all"),
        pytest.param(lambda s, c: s._mirror_emote(c, "discord", "all"), id="mirror-emote-all"),
        pytest.param(lambda s, c: s._mirror_emote_from(c, "discord", "all"), id="mirror-emote-from-all"),
        pytest.param(lambda s, c: s._unlink_all(c, "all"), id="unlink-all"),
        pytest.param(lambda s, c: s._unlink_channel(c, "all", "all"), id="unlink-channel-all"),
        pytest.param(lambda s, c: s._unlink_category(c, "all", "all"), id="unlink-category-all"),
        pytest.param(lambda s, c: s._unlink_role(c, "all", "all"), id="unlink-role-all"),
        pytest.param(lambda s, c: s._unlink_emote(c, "all", "all"), id="unlink-emote-all"),
        pytest.param(lambda s, c: s._unlink_user(c, "all", "all"), id="unlink-user-all"),
    ],
)
async def test_long_running_commands_get_a_cancel_reaction(call):
    sender = _sender(_Universal())
    ctx = _ctx()

    await call(sender, ctx)

    assert CANCEL_EMOJI in ctx.channel.sent[0].get("reactions", [])


@pytest.mark.parametrize(
    "call",
    [
        pytest.param(lambda s, c: s._mirror_channel(c, "discord", "general"), id="mirror-channel"),
        pytest.param(lambda s, c: s._mirror_role(c, "discord", "r1"), id="mirror-role"),
        pytest.param(lambda s, c: s._unlink_channel(c, "general", "discord"), id="unlink-channel"),
    ],
)
async def test_single_entity_commands_get_no_cancel_reaction(call):
    sender = _sender(_Universal())
    ctx = _ctx()

    await call(sender, ctx)

    assert "reactions" not in ctx.channel.sent[0]
