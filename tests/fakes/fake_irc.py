"""Reusable in-memory stand-in for the `irc` library's ServerConnection -
the only third-party object IrcSenderService/IrcReceiverService actually
call methods on (privmsg/notice/join/mode/whois/oper/is_connected).

test_irc_service.py predates this module and has its own narrower local
FakeConnection (whois/join/mode only, enough for the oper-check and
join_channel tests it covers) - this is a superset, for tests that also need
privmsg/notice (IrcReceiverService.receive(), STATUS/admin DM commands).
"""

from __future__ import annotations


class FakeIrcConnection:
    def __init__(self, connected: bool = True) -> None:
        self.privmsg_calls: list[tuple[str, str]] = []
        self.notice_calls: list[tuple[str, str]] = []
        self.whois_calls: list = []
        self.join_calls: list[str] = []
        self.mode_calls: list[tuple[str, str]] = []
        self.oper_calls: list[tuple[str, str]] = []
        self.disconnect_calls: list[str] = []
        self._connected = connected
        self._raises: BaseException | None = None

    def raise_on_privmsg(self, exc: BaseException) -> None:
        self._raises = exc

    def privmsg(self, target: str, text: str) -> None:
        if self._raises is not None:
            raise self._raises
        self.privmsg_calls.append((target, text))

    def notice(self, target: str, text: str) -> None:
        self.notice_calls.append((target, text))

    def whois(self, targets) -> None:
        self.whois_calls.append(targets)

    def join(self, channel: str) -> None:
        self.join_calls.append(channel)

    def mode(self, channel: str, modes: str) -> None:
        self.mode_calls.append((channel, modes))

    def oper(self, account: str, password: str) -> None:
        self.oper_calls.append((account, password))

    def disconnect(self, reason: str = "") -> None:
        self.disconnect_calls.append(reason)
        self._connected = False

    def is_connected(self) -> bool:
        return self._connected


class FakeIrcSource:
    def __init__(self, nick: str) -> None:
        self.nick = nick


class FakeIrcEvent:
    """Stands in for the `irc` library's Event, as passed to
    on_privmsg/on_pubmsg/on_pubnotice. `arguments` is the line's payload
    (index 0 is the message/notice text); `target` is only meaningful for
    pubmsg/pubnotice (the channel); `source` is only meaningful for
    privmsg/pubmsg (who sent it)."""

    def __init__(self, *, text: str, nick: str | None = None, target: str | None = None) -> None:
        self.arguments = [text]
        self.source = FakeIrcSource(nick) if nick is not None else None
        self.target = target
