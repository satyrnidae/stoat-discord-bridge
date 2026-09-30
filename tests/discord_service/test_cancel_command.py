"""Long-running Discord commands get a Cancel button on their "working"
placeholder (issue #200). Clicking it cancels the running linker call and the
placeholder ends up saying the command was canceled."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from stoat_discord_bridge.services import long_running
from stoat_discord_bridge.services.discord_service.editor import CancelView
from stoat_discord_bridge.services.long_running import CANCELED_TEXT, STILL_WORKING_TEXT, WORKING_TEXT
from tests.discord_service.conftest import FakeInteraction, _make_sender


class _Response:
    def __init__(self) -> None:
        self.edits: list[tuple[str | None, object]] = []
        self.messages: list[str] = []

    async def edit_message(self, *, content=None, view=None):
        self.edits.append((content, view))

    async def send_message(self, content, *, ephemeral=False):
        self.messages.append(content)


def _click(user_id: int = 1, manage_guild: bool = True):
    return SimpleNamespace(
        user=SimpleNamespace(id=user_id, guild_permissions=SimpleNamespace(manage_guild=manage_guild)),
        response=_Response(),
    )


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

    async def describe_group(self, **kwargs):
        return None


def _sender(linker):
    return _make_sender(
        linker, category_linker=linker, role_linker=linker, emote_linker=linker, user_linker=linker
    )


async def _blocked_forever():
    await asyncio.sleep(10)


# ---------------------------------------------------------------- CancelView


async def test_cancel_button_cancels_the_task_and_edits_the_message():
    task = asyncio.ensure_future(_blocked_forever())
    view = CancelView(task, invoker_id=1)
    click = _click()

    await view.cancel_button.callback(click)

    assert click.response.edits == [("Canceling...", None)]
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_cancel_button_rejects_anyone_but_the_invoker():
    task = asyncio.ensure_future(_blocked_forever())
    view = CancelView(task, invoker_id=1)
    click = _click(user_id=2)

    await view.cancel_button.callback(click)

    assert not task.cancelled() and not task.done()
    assert click.response.edits == []
    assert click.response.messages
    task.cancel()


async def test_cancel_button_after_the_command_finished_is_a_no_op():
    async def done():
        return "ok"

    task = asyncio.ensure_future(done())
    await task
    view = CancelView(task, invoker_id=1)
    click = _click()

    await view.cancel_button.callback(click)

    assert task.result() == "ok"
    assert click.response.edits == [(None, None)]


async def test_cancel_view_never_times_out():
    task = asyncio.get_running_loop().create_future()
    assert CancelView(task, invoker_id=1).timeout is None


# ---------------------------------------------------------------- handler wiring


def _cancel_view_attached(interaction: FakeInteraction) -> CancelView | None:
    return next((v for _, v in interaction.placeholder_edits if isinstance(v, CancelView)), None)


async def test_clicking_cancel_ends_the_command_with_the_canceled_text():
    linker = _Universal(block=True)
    sender = _sender(linker)
    interaction = FakeInteraction()

    run = asyncio.create_task(sender._handle_unlink_all(interaction, "all"))
    await linker.started.wait()
    view = _cancel_view_attached(interaction)
    assert view is not None
    await view.cancel_button.callback(_click())
    await run

    assert linker.canceled
    assert interaction.original_edits[-1] == CANCELED_TEXT
    assert view.is_finished()


async def test_normal_completion_edits_the_same_placeholder():
    sender = _sender(_Universal())
    interaction = FakeInteraction()

    await sender._handle_transfer_history(interaction, "import", "stoat", "01ABC", None, None)

    assert interaction.placeholder_edits[0][0] == WORKING_TEXT
    assert _cancel_view_attached(interaction) is not None
    # the result replaces the placeholder, dropping the Cancel button
    assert interaction.original_edits == ["ok"]
    assert interaction.sent_views == [None]


async def test_slow_completion_clears_the_button_and_posts_a_new_message(monkeypatch):
    monkeypatch.setattr(long_running, "SLOW_AFTER_SECONDS", 0.01)

    class _Slow(_Universal):
        def __getattr__(self, name):
            async def call(**kwargs):
                await asyncio.sleep(0.05)
                return "ok"

            return call

    sender = _sender(_Slow())
    interaction = FakeInteraction()

    await sender._handle_transfer_history(interaction, "import", "stoat", "01ABC", None, None)

    assert interaction.original_edits == [STILL_WORKING_TEXT]
    assert interaction.sent == [STILL_WORKING_TEXT, "ok"]
    assert interaction.placeholder_edits[-1] == (None, None)


async def test_a_quick_command_gets_no_cancel_button():
    sender = _sender(_Universal())
    interaction = FakeInteraction()

    await sender._handle_link_channel(interaction, "stoat", "01ABC", None)

    assert _cancel_view_attached(interaction) is None


@pytest.mark.parametrize(
    "call",
    [
        pytest.param(lambda s, i: s._handle_mirror_channel(i, "stoat", None, with_history=True), id="mirror-history"),
        pytest.param(
            lambda s, i: s._handle_mirror_channel_from(i, "stoat", "01ABC", with_history=True), id="mirror-from-history"
        ),
        pytest.param(lambda s, i: s._handle_mirror_channel(i, "stoat", "all"), id="mirror-channel-all"),
        pytest.param(lambda s, i: s._handle_mirror_channel_from(i, "stoat", "all"), id="mirror-channel-from-all"),
        pytest.param(lambda s, i: s._handle_mirror_channel(i, "all", None), id="mirror-channel-to-all"),
        pytest.param(lambda s, i: s._handle_transfer_history(i, "import", "stoat", "01ABC", None, None), id="import"),
        pytest.param(lambda s, i: s._handle_transfer_history(i, "export", "stoat", "01ABC", None, None), id="export"),
        pytest.param(lambda s, i: s._handle_mirror_category(i, "stoat", "123"), id="mirror-category"),
        pytest.param(lambda s, i: s._handle_mirror_category_from(i, "stoat", "01ABC"), id="mirror-category-from"),
        pytest.param(lambda s, i: s._handle_mirror_role(i, "all", "stoat"), id="mirror-role-all"),
        pytest.param(lambda s, i: s._handle_mirror_role_from(i, "stoat", "all"), id="mirror-role-from-all"),
        pytest.param(lambda s, i: s._handle_mirror_emote(i, "all", "stoat"), id="mirror-emote-all"),
        pytest.param(lambda s, i: s._handle_mirror_emote_from(i, "stoat", "all"), id="mirror-emote-from-all"),
        pytest.param(lambda s, i: s._handle_unlink_all(i, "all"), id="unlink-all"),
        pytest.param(lambda s, i: s._handle_unlink_channel(i, "all", "all"), id="unlink-channel-all"),
        pytest.param(lambda s, i: s._handle_unlink_category(i, "all", "all"), id="unlink-category-all"),
        pytest.param(lambda s, i: s._handle_unlink_role(i, "all", "all"), id="unlink-role-all"),
        pytest.param(lambda s, i: s._handle_unlink_emote(i, "all", "all"), id="unlink-emote-all"),
        pytest.param(lambda s, i: s._handle_unlink_user(i, "all", "all"), id="unlink-user-all"),
    ],
)
async def test_long_running_commands_get_a_cancel_button(call):
    sender = _sender(_Universal())
    interaction = FakeInteraction()

    await call(sender, interaction)

    assert _cancel_view_attached(interaction) is not None


@pytest.mark.parametrize(
    "call",
    [
        pytest.param(lambda s, i: s._handle_mirror_channel(i, "stoat", None), id="mirror-channel"),
        pytest.param(lambda s, i: s._handle_mirror_role(i, "r1", "stoat"), id="mirror-role"),
        pytest.param(lambda s, i: s._handle_unlink_channel(i, "stoat", None), id="unlink-channel"),
    ],
)
async def test_single_entity_commands_get_no_cancel_button(call):
    sender = _sender(_Universal())
    interaction = FakeInteraction()

    await call(sender, interaction)

    assert _cancel_view_attached(interaction) is None
