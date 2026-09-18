from __future__ import annotations

import asyncio
import inspect
import math
from collections.abc import Awaitable, Callable
from typing import Generic, ParamSpec, TypeVar, cast

from .core import Context, TaskPolicy


P = ParamSpec("P")
R = TypeVar("R")


def _validate_delay(delay: float) -> None:
    if not math.isfinite(delay) or delay < 0:
        raise ValueError("timer delay must be a finite, non-negative number")


async def _invoke(
    callback: Callable[P, R | Awaitable[R]], *args: P.args, **kwargs: P.kwargs
) -> R:
    result = callback(*args, **kwargs)
    return cast(R, await result) if inspect.isawaitable(result) else result


class Debounced(Generic[P, R]):
    def __init__(
        self,
        timer: TimerService,
        delay: float,
        callback: Callable[P, R | Awaitable[R]],
    ) -> None:
        _validate_delay(delay)
        self._timer = timer
        self._delay = delay
        self._callback = callback
        self._task: asyncio.Task[R] | None = None

    def __call__(self, *args: P.args, **kwargs: P.kwargs) -> asyncio.Task[R]:
        if self._task is not None and not self._task.done():
            self._task.cancel()

        async def run() -> R:
            await asyncio.sleep(self._delay)
            return await _invoke(self._callback, *args, **kwargs)

        self._task = self._timer.ctx.spawn(run(), policy="cancel")
        return self._task

    def cancel(self) -> None:
        if self._task is not None:
            self._task.cancel()


class Throttled(Generic[P, R]):
    def __init__(
        self,
        timer: TimerService,
        delay: float,
        callback: Callable[P, R | Awaitable[R]],
    ) -> None:
        _validate_delay(delay)
        self._timer = timer
        self._delay = delay
        self._callback = callback
        self._lock = asyncio.Lock()
        self._next = 0.0

    def __call__(self, *args: P.args, **kwargs: P.kwargs) -> asyncio.Task[R]:
        async def run() -> R:
            async with self._lock:
                loop = asyncio.get_running_loop()
                await asyncio.sleep(max(0.0, self._next - loop.time()))
                self._next = loop.time() + self._delay
                return await _invoke(self._callback, *args, **kwargs)

        return self._timer.ctx.spawn(run(), policy="cancel")


class TimerService:
    def __init__(self, ctx: Context) -> None:
        self.ctx = ctx

    def timeout(
        self,
        delay: float,
        callback: Callable[[], R | Awaitable[R]],
        *,
        policy: TaskPolicy = "cancel",
    ) -> asyncio.Task[R]:
        _validate_delay(delay)

        async def run() -> R:
            await asyncio.sleep(delay)
            return await _invoke(callback)

        return self.ctx.spawn(run(), policy=policy)

    def interval(
        self,
        delay: float,
        callback: Callable[[], object],
        *,
        immediate: bool = False,
        policy: TaskPolicy = "cancel",
    ) -> asyncio.Task[None]:
        _validate_delay(delay)

        async def run() -> None:
            if immediate:
                await _invoke(callback)
            while True:
                await asyncio.sleep(delay)
                await _invoke(callback)

        return self.ctx.spawn(run(), policy=policy)

    def debounce(
        self, delay: float, callback: Callable[P, R | Awaitable[R]]
    ) -> Debounced[P, R]:
        return Debounced(self, delay, callback)

    def throttle(
        self, delay: float, callback: Callable[P, R | Awaitable[R]]
    ) -> Throttled[P, R]:
        return Throttled(self, delay, callback)
