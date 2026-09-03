"""Central logging configuration for the bridge process.

Every module logs through `logging.getLogger(__name__)` rather than
`print()`, so output is timestamped, filterable by level, and (for
exceptions) carries a real traceback. `configure_logging()` is called once,
from `__main__.main()`, before anything else starts.

Verbosity is controlled by the `LOG_LEVEL` env var (default `INFO`) - see
.env.example. The bundled third-party clients (discord.py, stoat.py's
websockets transport, aiohttp) are considerably chattier than this bridge's
own code at DEBUG, so their loggers are pinned to `LOG_LEVEL` only when it's
DEBUG or lower; otherwise they're capped at WARNING regardless of the
bridge's own level, to keep INFO-level output readable.

Secret redaction (issue #59): a `_RedactingFormatter` sits on the stream
handler and scrubs anything sensitive out of every line - message, args, and
exception traceback alike - before it reaches stderr (and hence `docker
logs`). Two layers, always on:

* **Known secret values.** `register_config_secrets()` is called from
  `__main__.main()` right after `load_config()` and feeds
  `register_secret_values()` every credential the config resolved - bot
  tokens, the IRC NickServ/OPER passwords, and any userinfo embedded in the
  Mongo URI - plus the 1Password service-account token if one is in the
  environment. Each is replaced by `***` wherever it appears verbatim.
* **Credential-bearing IRC protocol lines.** The `irc` library logs raw
  `TO SERVER:` / `FROM SERVER:` lines at DEBUG; `OPER`, `PASS`, and
  `NickServ IDENTIFY` carry a password inline regardless of whether that
  exact value was registered, so their argument is pattern-redacted too.

A third, opt-in layer redacts raw platform ids (Discord snowflakes, Stoat
ULIDs) - enable with `LOG_REDACT_IDS=1`. It's off by default because ids are
not credentials and appear in almost every diagnostic line; redacting them
makes live-server debugging much harder.
"""

from __future__ import annotations

import logging
import os
import re
import sys
from urllib.parse import unquote, urlsplit

_DEFAULT_LEVEL = "INFO"

_REDACTED = "***"

# Third-party loggers that are only interesting for deep protocol-level
# debugging - left at WARNING unless the whole bridge is running at DEBUG.
_NOISY_THIRD_PARTY_LOGGERS = ("discord", "aiohttp", "websockets", "irc")

# Verbatim secret strings to scrub from every log line, populated by
# register_secret_values() once config has been resolved, and the same set
# pre-sorted longest-first (so an overlapping substring can't leave a fragment
# behind) - rebuilt on each registration rather than re-sorted per log line.
_secret_values: set[str] = set()
_secret_values_by_length: tuple[str, ...] = ()

# A registered value shorter than this is ignored - too likely to be a common
# substring (a "/" command prefix, a two-letter oper name) whose blanket
# redaction would mangle unrelated lines rather than protect anything.
_MIN_SECRET_LEN = 5

# Credential-bearing IRC commands as they appear in the `irc` library's raw
# protocol echo - it logs every sent/received line through its logger as
# "TO SERVER: <line>" / "FROM SERVER: <line>" at DEBUG. Three carry a
# password: `OPER <name> <password>`, bare `PASS <password>`, and
# `PRIVMSG NickServ :IDENTIFY [account] <password>` (or a bare `IDENTIFY`).
# Gated on the "SERVER:" marker so this only fires on a real protocol echo -
# an ordinary log line that happens to contain the word "OPER"/"identify" is
# left alone - and, once matched, everything from the credential to
# end-of-line is redacted in one go (`OPER`'s <name> is kept; `IDENTIFY`'s
# optional <account> is not - erring safe beats trying to tell account and
# password apart when the password needn't be the last token).
_IRC_CRED_RE = re.compile(
    r"(SERVER:.*?\b(?:OPER\s+\S+\s+|PASS\s+|IDENTIFY\s+))\S.*$",
    re.IGNORECASE | re.MULTILINE,
)

# Discord snowflakes (17-20 digits) and Stoat/ULID ids (26 Crockford-base32
# chars). Only consulted when LOG_REDACT_IDS is set.
_SNOWFLAKE_RE = re.compile(r"\b\d{17,20}\b")
_ULID_RE = re.compile(r"\b[0-7][0-9A-HJKMNP-TV-Z]{25}\b")


def register_secret_values(*values: object) -> None:
    """Register credential strings to scrub from all subsequent log output.

    Accepts anything (a `None`, an `int` id, a whole config object's fields);
    only non-empty strings of a plausible length are kept. A Mongo URI is
    additionally split so its embedded `user:pass@` userinfo is redacted even
    though the full URI never appears verbatim in a log line.
    """
    global _secret_values_by_length
    for value in values:
        if not isinstance(value, str) or len(value) < _MIN_SECRET_LEN:
            continue
        _secret_values.add(value)
        if "://" in value:
            try:
                split = urlsplit(value)
            except ValueError:
                continue
            for part in (split.username, split.password):
                for form in {part, unquote(part)} if part else ():
                    if len(form) >= _MIN_SECRET_LEN:
                        _secret_values.add(form)
    _secret_values_by_length = tuple(sorted(_secret_values, key=len, reverse=True))


def _redact(text: str) -> str:
    for secret in _secret_values_by_length:
        if secret in text:
            text = text.replace(secret, _REDACTED)

    text = _IRC_CRED_RE.sub(lambda m: m.group(1) + _REDACTED, text)

    if _redact_ids_enabled():
        text = _SNOWFLAKE_RE.sub(_REDACTED, text)
        text = _ULID_RE.sub(_REDACTED, text)

    return text


def _redact_ids_enabled() -> bool:
    return os.environ.get("LOG_REDACT_IDS", "").strip().lower() in ("1", "true", "yes", "on")


class _RedactingFormatter(logging.Formatter):
    """Formats a record normally, then scrubs secrets from the whole line -
    message, args, and any appended exception/stack trace included."""

    def format(self, record: logging.LogRecord) -> str:
        return _redact(super().format(record))


def configure_logging(level: str | None = None) -> None:
    level_name = (level or os.environ.get("LOG_LEVEL") or _DEFAULT_LEVEL).upper()
    resolved = logging.getLevelName(level_name)
    if not isinstance(resolved, int):
        resolved = logging.INFO
        level_name = "INFO"

    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(
        _RedactingFormatter(
            fmt="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S%z",
        )
    )

    root = logging.getLogger()
    root.setLevel(resolved)
    root.handlers.clear()
    root.addHandler(handler)

    if resolved > logging.DEBUG:
        for name in _NOISY_THIRD_PARTY_LOGGERS:
            logging.getLogger(name).setLevel(logging.WARNING)

    logging.getLogger(__name__).info("logging configured at %s", level_name)


def register_config_secrets(config: object) -> None:
    """Pull every credential out of a resolved `BridgeConfig` and register it
    for redaction. Tolerant of a partial/duck-typed object so tests can pass a
    stub."""
    for connector in (
        *getattr(config, "discord", []),
        *getattr(config, "stoat", []),
    ):
        register_secret_values(getattr(connector, "bot_token", None))
    for connector in getattr(config, "irc", []):
        register_secret_values(
            getattr(connector, "nickserv_password", None),
            getattr(connector, "oper_password", None),
        )
    mongo = getattr(config, "mongo", None)
    if mongo is not None:
        register_secret_values(getattr(mongo, "uri", None))
    register_secret_values(
        os.environ.get("OP_SERVICE_ACCOUNT_TOKEN"),
        os.environ.get("MONGO__URI"),
        os.environ.get("MONGODB_URI"),
    )
