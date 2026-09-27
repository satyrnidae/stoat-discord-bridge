"""Status-message timing for long-running admin commands (issue #201).

A command reply starts as a placeholder that's edited in place with the result.
Past `SLOW_AFTER_SECONDS` the placeholder is flipped once to `STILL_WORKING_TEXT`
and the result is posted as a fresh message instead, since an old placeholder
isn't reliably editable (and a fresh message notifies the operator).
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
STILL_WORKING_TEXT = "Still working on this - I'll post the result in a new message once it's done."


async def watch_long_running(
    coro: Awaitable[T],
    *,
    on_slow: Callable[[], Awaitable[None]],
    slow_after: float = SLOW_AFTER_SECONDS,
) -> tuple[T, bool]:
    """Await `coro`, calling `on_slow()` once if it's still running after
    `slow_after` seconds. Returns `(result, went_slow)`; `coro`'s own
    exception propagates, and a failing `on_slow` is logged and ignored.
    Canceling the caller cancels `coro` too."""
    task = asyncio.ensure_future(coro)
    try:
        done, _ = await asyncio.wait({task}, timeout=slow_after)
        went_slow = not done
        if went_slow:
            try:
                await on_slow()
            except Exception:
                logger.warning("long-running status update failed", exc_info=True)
        return await task, went_slow
    except asyncio.CancelledError:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        raise
