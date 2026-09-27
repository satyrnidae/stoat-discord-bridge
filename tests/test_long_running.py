import asyncio

import pytest

from stoat_discord_bridge.services.long_running import watch_long_running


class _Recorder:
    def __init__(self, *, raises: bool = False) -> None:
        self.calls = 0
        self._raises = raises

    async def __call__(self) -> None:
        self.calls += 1
        if self._raises:
            raise RuntimeError("status edit failed")


async def _finish_after(seconds: float, value: str = "done") -> str:
    await asyncio.sleep(seconds)
    return value


async def test_fast_operation_never_fires_on_slow():
    on_slow = _Recorder()
    result, went_slow = await watch_long_running(_finish_after(0, "ok"), on_slow=on_slow, slow_after=0.05)
    assert result == "ok"
    assert went_slow is False
    assert on_slow.calls == 0


async def test_slow_operation_fires_on_slow_exactly_once():
    on_slow = _Recorder()
    result, went_slow = await watch_long_running(_finish_after(0.2, "ok"), on_slow=on_slow, slow_after=0.03)
    assert result == "ok"
    assert went_slow is True
    assert on_slow.calls == 1


async def test_operation_exception_propagates():
    async def boom() -> str:
        await asyncio.sleep(0)
        raise ValueError("nope")

    with pytest.raises(ValueError, match="nope"):
        await watch_long_running(boom(), on_slow=_Recorder(), slow_after=0.05)


async def test_slow_operation_exception_still_propagates():
    async def slow_boom() -> str:
        await asyncio.sleep(0.1)
        raise ValueError("late")

    on_slow = _Recorder()
    with pytest.raises(ValueError, match="late"):
        await watch_long_running(slow_boom(), on_slow=on_slow, slow_after=0.02)
    assert on_slow.calls == 1


async def test_failing_on_slow_does_not_abort_the_operation():
    on_slow = _Recorder(raises=True)
    result, went_slow = await watch_long_running(_finish_after(0.1, "ok"), on_slow=on_slow, slow_after=0.02)
    assert result == "ok"
    assert went_slow is True


async def test_cancelling_the_watcher_cancels_the_operation():
    started = asyncio.Event()
    canceled = asyncio.Event()

    async def blocker() -> str:
        started.set()
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            canceled.set()
            raise
        return "unreachable"

    watcher = asyncio.ensure_future(watch_long_running(blocker(), on_slow=_Recorder(), slow_after=5))
    await started.wait()
    watcher.cancel()
    with pytest.raises(asyncio.CancelledError):
        await watcher
    assert canceled.is_set()
