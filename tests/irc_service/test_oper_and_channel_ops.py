from __future__ import annotations

import asyncio

import irc.client

from tests.irc_service.conftest import FakeConnection, _make_sender, _patch_connection


# ---------------------------------------------------------------- ident / username kwarg passthrough
#
# _IrcClient hands `username` (and `connect_factory`) to
# SingleServerIRCBot.__init__ as bare **connect_params, trusting the library
# to splat them into ServerConnection.connect(). These pin that contract to
# irc 20.5.0 by driving a real _IrcClient._connect() with
# ServerConnection.connect patched to record its arguments.


def _capture_connect(monkeypatch) -> dict:
    captured: dict = {}

    def _record(self, server, port, nickname, password=None, **kwargs):
        captured.update(server=server, port=port, nickname=nickname, password=password, **kwargs)

    monkeypatch.setattr(irc.client.ServerConnection, "connect", _record)
    return captured


async def test_ident_is_forwarded_as_the_connect_username(monkeypatch):
    captured = _capture_connect(monkeypatch)
    sender = _make_sender(nick="bot", ident="bridged")

    sender._client._connect()

    assert captured["username"] == "bridged"
    assert captured["nickname"] == "bot"
    assert captured["ircname"] == "bot"  # realname -> ircname on the USER line
    assert "connect_factory" in captured  # rides the same passthrough


async def test_no_ident_omits_username_and_defaults_to_the_nick(monkeypatch):
    captured = _capture_connect(monkeypatch)
    sender = _make_sender(nick="bot", ident=None)

    sender._client._connect()

    # ServerConnection.connect defaults `username` to the nickname when unset,
    # so passing nothing is the intended no-op.
    assert "username" not in captured


# ---------------------------------------------------------------- _check_is_oper / _resolve_whois


async def test_check_is_oper_true_on_whoisoperator(monkeypatch):
    sender = _make_sender()
    conn = FakeConnection()
    _patch_connection(monkeypatch, sender, conn)

    task = asyncio.create_task(sender._check_is_oper("Alice"))
    await asyncio.sleep(0)  # let _check_is_oper run up to awaiting the pending future
    sender._resolve_whois("Alice", True)

    assert await task is True
    assert conn.whois_calls == [["Alice"]]


async def test_check_is_oper_false_on_endofwhois_alone(monkeypatch):
    sender = _make_sender()
    _patch_connection(monkeypatch, sender, FakeConnection())

    task = asyncio.create_task(sender._check_is_oper("Bob"))
    await asyncio.sleep(0)
    sender._resolve_whois("Bob", False)  # on_endofwhois, no prior on_whoisoperator

    assert await task is False


async def test_check_is_oper_true_survives_a_trailing_endofwhois_false(monkeypatch):
    """on_whoisoperator (True) always arrives before on_endofwhois (False)
    for the same exchange when the target IS an oper - the trailing False
    call must be a no-op, not overwrite the already-resolved True."""
    sender = _make_sender()
    _patch_connection(monkeypatch, sender, FakeConnection())

    task = asyncio.create_task(sender._check_is_oper("Carol"))
    await asyncio.sleep(0)
    sender._resolve_whois("Carol", True)
    sender._resolve_whois("Carol", False)  # trailing on_endofwhois - must not clobber True

    assert await task is True


async def test_check_is_oper_nick_matching_is_case_insensitive(monkeypatch):
    sender = _make_sender()
    _patch_connection(monkeypatch, sender, FakeConnection())

    task = asyncio.create_task(sender._check_is_oper("DAVE"))
    await asyncio.sleep(0)
    sender._resolve_whois("dave", True)  # server echoes a differently-cased nick

    assert await task is True


async def test_resolve_whois_with_no_pending_future_is_a_noop(monkeypatch):
    sender = _make_sender()
    _patch_connection(monkeypatch, sender, FakeConnection())
    sender._resolve_whois("NoOneAsked", True)  # must not raise


async def test_check_is_oper_cleans_up_pending_entry(monkeypatch):
    sender = _make_sender()
    _patch_connection(monkeypatch, sender, FakeConnection())

    task = asyncio.create_task(sender._check_is_oper("Eve"))
    await asyncio.sleep(0)
    assert "eve" in sender._pending_whois
    sender._resolve_whois("Eve", True)
    await task
    assert "eve" not in sender._pending_whois


# ---------------------------------------------------------------- join_channel / default_channel_modes


async def test_join_channel_applies_default_modes_to_a_new_channel(monkeypatch):
    sender = _make_sender(default_channel_modes="+nt")
    conn = FakeConnection()
    _patch_connection(monkeypatch, sender, conn)

    await sender.join_channel("#newchan")

    assert conn.join_calls == ["#newchan"]
    assert conn.mode_calls == [("#newchan", "+nt")]


async def test_join_channel_skips_default_modes_on_a_channel_already_known(monkeypatch):
    sender = _make_sender(default_channel_modes="+nt")
    sender._channels.append("#existing")
    conn = FakeConnection()
    _patch_connection(monkeypatch, sender, conn)

    await sender.join_channel("#existing")

    assert conn.join_calls == ["#existing"]
    assert conn.mode_calls == []  # not "new" this session - no MODE sent


async def test_join_channel_no_modes_configured_sends_no_mode(monkeypatch):
    sender = _make_sender(default_channel_modes=None)
    conn = FakeConnection()
    _patch_connection(monkeypatch, sender, conn)

    await sender.join_channel("#newchan")

    assert conn.mode_calls == []


async def test_join_channel_does_nothing_while_disconnected(monkeypatch):
    sender = _make_sender(default_channel_modes="+nt")
    conn = FakeConnection(connected=False)
    _patch_connection(monkeypatch, sender, conn)

    await sender.join_channel("#newchan")

    assert conn.join_calls == []
    assert conn.mode_calls == []
    assert "#newchan" in sender._channels  # still tracked for a later reconnect


# ---------------------------------------------------------------- part_channel


async def test_part_channel_parts_a_tracked_channel_and_forgets_it(monkeypatch):
    sender = _make_sender()
    sender._channels.append("#synced")
    sender._pending_permanent_modes.add("#synced")
    conn = FakeConnection()
    _patch_connection(monkeypatch, sender, conn)

    await sender.part_channel("#synced", "Discord 'general'")

    assert conn.part_calls == ["#synced"]
    assert conn.privmsg_calls == [("#synced", "This channel was unlinked from Discord 'general'.")]
    assert "#synced" not in sender._channels
    assert sender._pending_permanent_modes == set()


async def test_part_channel_unknown_channel_is_a_noop(monkeypatch):
    sender = _make_sender()
    conn = FakeConnection()
    _patch_connection(monkeypatch, sender, conn)

    await sender.part_channel("#never-joined")

    assert conn.part_calls == []


async def test_part_channel_while_disconnected_still_forgets_channel(monkeypatch):
    sender = _make_sender()
    sender._channels.append("#synced")
    conn = FakeConnection(connected=False)
    _patch_connection(monkeypatch, sender, conn)

    await sender.part_channel("#synced")

    assert conn.part_calls == []
    assert "#synced" not in sender._channels


# ---------------------------------------------------------------- +P permanent-mode handling


async def test_permanent_mode_deferred_until_oper_then_applied(monkeypatch):
    sender = _make_sender(default_channel_modes="+HtnPR")
    conn = FakeConnection()
    _patch_connection(monkeypatch, sender, conn)

    await sender.join_channel("#synced")

    # base modes go out immediately; +P is held back until OPER confirms
    assert conn.mode_calls == [("#synced", "+HtnR")]
    assert sender._pending_permanent_modes == {"#synced"}

    sender._handle_youreoper()

    assert sender._is_oper is True
    assert conn.mode_calls == [("#synced", "+HtnR"), ("#synced", "+P")]
    assert sender._pending_permanent_modes == set()


async def test_permanent_mode_applied_inline_when_already_oper(monkeypatch):
    sender = _make_sender(default_channel_modes="+ntP")
    conn = FakeConnection()
    _patch_connection(monkeypatch, sender, conn)
    sender._is_oper = True

    await sender.join_channel("#synced")

    assert conn.mode_calls == [("#synced", "+nt"), ("#synced", "+P")]
    assert sender._pending_permanent_modes == set()


async def test_permanent_mode_skipped_for_thread_channels(monkeypatch):
    sender = _make_sender(default_channel_modes="+ntP")
    conn = FakeConnection()
    _patch_connection(monkeypatch, sender, conn)
    sender._is_oper = True

    channel = await sender.ensure_channel("My Thread", is_thread_category=True)

    assert channel == "#my-thread"
    assert conn.mode_calls == [("#my-thread", "+nt")]  # no +P
    assert sender._pending_permanent_modes == set()


async def test_no_permanent_mode_when_p_not_configured(monkeypatch):
    sender = _make_sender(default_channel_modes="+nt")
    conn = FakeConnection()
    _patch_connection(monkeypatch, sender, conn)

    await sender.join_channel("#synced")
    sender._handle_youreoper()

    assert conn.mode_calls == [("#synced", "+nt")]
    assert sender._pending_permanent_modes == set()


async def test_permanent_mode_p_only_config_sends_no_base_mode(monkeypatch):
    sender = _make_sender(default_channel_modes="+P")
    conn = FakeConnection()
    _patch_connection(monkeypatch, sender, conn)

    await sender.join_channel("#synced")

    assert conn.mode_calls == []
    assert sender._pending_permanent_modes == {"#synced"}


async def test_handle_youreoper_with_nothing_pending_is_a_noop(monkeypatch):
    sender = _make_sender()
    conn = FakeConnection()
    _patch_connection(monkeypatch, sender, conn)

    sender._handle_youreoper()

    assert sender._is_oper is True
    assert conn.mode_calls == []


async def test_disconnect_clears_oper_flag(monkeypatch):
    sender = _make_sender()
    sender._is_oper = True

    sender._handle_disconnect()

    assert sender._is_oper is False


