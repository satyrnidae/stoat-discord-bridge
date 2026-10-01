"""Status-message timing for long-running admin commands (issue #201).

A command reply starts as a placeholder that's edited in place with the result.
Past `SLOW_AFTER_SECONDS` the placeholder is flipped once to `STILL_WORKING_TEXT`
so the operator knows the bot hasn't stalled; the result still replaces it, so
a command leaves one message (issue #197). A new message goes out only if that
edit fails.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from typing import TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")

SLOW_AFTER_SECONDS = 60.0
WORKING_TEXT = "Working on it..."
STILL_WORKING_TEXT = "Still working on this - I'll update this message once it's done."
CANCELED_TEXT = "Canceled. Anything already done before the cancel stays in place."


class CommandCanceled(Exception):
    """The operator canceled a long-running command (issue #200) - its
    operation task was canceled while the caller itself wasn't."""


async def watch_long_running(
    coro: Awaitable[T],
    *,
    on_slow: Callable[[], Awaitable[None]],
    slow_after: float | None = None,
) -> T:
    """Await `coro`, calling `on_slow()` once if it's still running after
    `slow_after` seconds (default `SLOW_AFTER_SECONDS`, read at call time so
    tests can shorten it) - `on_slow` is where a caller records that the
    command went slow, since an error from `coro` still has to know it.
    `coro`'s own exception propagates, and a failing `on_slow` is logged and
    ignored. Canceling the caller cancels `coro` too.

    `coro` may be an already-started task, so a cancel control can hold it:
    canceling that task (rather than the caller) raises `CommandCanceled`."""
    task = asyncio.ensure_future(coro)
    try:
        timeout = SLOW_AFTER_SECONDS if slow_after is None else slow_after
        done, _ = await asyncio.wait({task}, timeout=timeout)
        if not done:
            try:
                await on_slow()
            except Exception:
                logger.warning("long-running status update failed", exc_info=True)
        return await task
    except asyncio.CancelledError:
        current = asyncio.current_task()
        if task.cancelled() and not (current is not None and current.cancelling()):
            raise CommandCanceled from None
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        raise
