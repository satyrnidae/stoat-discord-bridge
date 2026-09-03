"""logging_setup.py's secret redaction (issue #59).

Exercises `_RedactingFormatter` end to end - a real `LogRecord` formatted
through it - so message, %-args, and exception tracebacks are all covered.
"""

from __future__ import annotations

import logging

import pytest

from stoat_discord_bridge import logging_setup
from stoat_discord_bridge.logging_setup import (
    _RedactingFormatter,
    register_config_secrets,
    register_secret_values,
)


@pytest.fixture(autouse=True)
def _clean_secret_registry(monkeypatch):
    monkeypatch.setattr(logging_setup, "_secret_values", set())
    monkeypatch.delenv("LOG_REDACT_IDS", raising=False)


def _format(msg, *args, exc_info=None, name="stoat_discord_bridge.test"):
    record = logging.LogRecord(
        name=name,
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg=msg,
        args=args,
        exc_info=exc_info,
    )
    return _RedactingFormatter(fmt="%(levelname)s %(name)s: %(message)s").format(record)


def test_registered_token_is_redacted_in_message_and_args():
    register_secret_values("super-secret-bot-token")
    assert "super-secret-bot-token" not in _format("token is %s", "super-secret-bot-token")
    assert "super-secret-bot-token" not in _format("token is super-secret-bot-token inline")
    assert "***" in _format("token is %s", "super-secret-bot-token")


def test_short_values_are_not_registered():
    register_secret_values("/", "!?", "abcd")
    out = _format("prefix is %s and %s", "/", "abcd")
    assert out.endswith("prefix is / and abcd")


def test_irc_oper_line_password_is_redacted_even_if_not_registered():
    out = _format("[irc:net] TO SERVER: %s", "OPER bridgeadmin hunter2public")
    assert "hunter2public" not in out
    assert "OPER bridgeadmin ***" in out


def test_ordinary_lines_mentioning_oper_are_left_alone():
    # The password patterns are end-of-line anchored so status/info lines that
    # merely contain the word aren't mangled.
    for line in (
        "[irc:net] OPER confirmed",
        "[irc:net] OPER confirmed - applying +P to #chan, #other",
        "[irc:net] WHOIS for someone timed out - treating as not-oper",
    ):
        assert _format("%s", line).endswith(line)


def test_bare_pass_line_is_redacted():
    out = _format("TO SERVER: PASS s3rv3rp4ss")
    assert "s3rv3rp4ss" not in out
    assert "PASS ***" in out


def test_nickserv_identify_password_is_redacted():
    with_account = _format("TO SERVER: PRIVMSG NickServ :IDENTIFY botnick nickservpw123")
    assert "nickservpw123" not in with_account
    assert "IDENTIFY botnick ***" in with_account

    without_account = _format("TO SERVER: PRIVMSG NickServ :IDENTIFY nickservpw123")
    assert "nickservpw123" not in without_account
    assert "IDENTIFY ***" in without_account


def test_exception_traceback_is_redacted():
    register_secret_values("tok-in-traceback-value")
    try:
        raise RuntimeError("auth failed for tok-in-traceback-value")
    except RuntimeError:
        import sys

        out = _format("relay failed", exc_info=sys.exc_info())
    assert "tok-in-traceback-value" not in out
    assert "Traceback" in out


def test_ids_are_kept_by_default_but_redacted_when_opted_in(monkeypatch):
    snowflake = "123456789012345678"
    ulid = "01J8ZQK7N3AB2CD4EF6GH8JK9M"
    line = f"linked channel {snowflake} to {ulid}"

    assert snowflake in _format("%s", line)

    monkeypatch.setenv("LOG_REDACT_IDS", "1")
    redacted = _format("%s", line)
    assert snowflake not in redacted
    assert ulid not in redacted


def test_mongo_uri_userinfo_is_redacted_from_a_partial_uri_log():
    register_secret_values("mongodb://bridgeuser:s3cretp%40ss@db.example:27017/bridge")
    # A line that logs only the host part still leaks nothing, and a line that
    # happens to echo the decoded password is caught too.
    assert "s3cretp@ss" not in _format("mongo auth: user=%s pw=%s", "bridgeuser", "s3cretp@ss")
    assert "bridgeuser" not in _format("connecting as %s", "bridgeuser")


def test_register_config_secrets_pulls_from_a_duck_typed_config():
    class _C:
        class _Conn:
            def __init__(self, **kw):
                self.__dict__.update(kw)

        discord = [_Conn(bot_token="discord-token-abcdef")]
        stoat = [_Conn(bot_token="stoat-token-ghijkl")]
        irc = [_Conn(nickserv_password="ns-pw-mnopqr", oper_password="oper-pw-stuvwx")]
        mongo = _Conn(uri="mongodb://localhost:27017")

    register_config_secrets(_C())
    out = _format(
        "%s %s %s %s",
        "discord-token-abcdef",
        "stoat-token-ghijkl",
        "ns-pw-mnopqr",
        "oper-pw-stuvwx",
    )
    for secret in ("discord-token-abcdef", "stoat-token-ghijkl", "ns-pw-mnopqr", "oper-pw-stuvwx"):
        assert secret not in out
