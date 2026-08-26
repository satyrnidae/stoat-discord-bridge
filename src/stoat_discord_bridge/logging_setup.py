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
"""

from __future__ import annotations

import logging
import os
import sys

_DEFAULT_LEVEL = "INFO"

# Third-party loggers that are only interesting for deep protocol-level
# debugging - left at WARNING unless the whole bridge is running at DEBUG.
_NOISY_THIRD_PARTY_LOGGERS = ("discord", "aiohttp", "websockets", "irc")


def configure_logging(level: str | None = None) -> None:
    level_name = (level or os.environ.get("LOG_LEVEL") or _DEFAULT_LEVEL).upper()
    resolved = logging.getLevelName(level_name)
    if not isinstance(resolved, int):
        resolved = logging.INFO
        level_name = "INFO"

    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(
        logging.Formatter(
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
