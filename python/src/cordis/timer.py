from __future__ import annotations

import asyncio
import inspect
import math
from collections.abc import Awaitable, Callable
from types import TracebackType
from typing import Generic, ParamSpec, Self, TypeVar, cast, overload

from .core import Context, InactiveContextError, TaskPolicy


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
        *,
        trailing: bool = True,
    ) -> None:
        _validate_delay(delay)
        self._timer = timer
        self._delay = delay
        self._callback = callback
        self._allow_trailing = trailing
        self._task: asyncio.Task[R | None] | None = None
        self._trailing: asyncio.Task[R | None] | None = None
        self._last_called = -float("inf")

    def __call__(self, *args: P.args, **kwargs: P.kwargs) -> asyncio.Task[R | None]:
        loop = asyncio.get_running_loop()
        now = loop.time()
        remaining = self._delay - (now - self._last_called)

        if remaining <= 0:
            self._last_called = now
            if self._trailing is not None and not self._trailing.done():
                self._trailing.cancel()

            async def run_leading() -> R | None:
                return await _invoke(self._callback, *args, **kwargs)

            self._task = self._timer.ctx.spawn(run_leading(), policy="cancel")
            return self._task

        if not self._allow_trailing:

            async def skipped() -> None:
                return None

            return self._timer.ctx.spawn(skipped(), policy="cancel")

        if self._trailing is not None and not self._trailing.done():
            self._trailing.cancel()

        async def run_trailing() -> R | None:
            await asyncio.sleep(remaining)
            self._last_called = asyncio.get_running_loop().time()
            return await _invoke(self._callback, *args, **kwargs)

        self._trailing = self._timer.ctx.spawn(run_trailing(), policy="cancel")
        return self._trailing

    def cancel(self) -> None:
        if self._task is not None and not self._task.done():
            self._task.cancel()
        if self._trailing is not None and not self._trailing.done():
            self._trailing.cancel()


class TimerService:
    def __init__(self, ctx: Context) -> None:
        self.ctx = ctx

    @overload
    def timeout(
        self, delay: float, *, policy: TaskPolicy = "cancel"
    ) -> asyncio.Task[None]: ...

    @overload
    def timeout(
        self,
        delay: float,
        callback: Callable[[], R | Awaitable[R]],
        *,
        policy: TaskPolicy = "cancel",
    ) -> asyncio.Task[R]: ...

    def timeout(
        self,
        delay: float,
        callback: Callable[[], R | Awaitable[R]] | None = None,
        *,
        policy: TaskPolicy = "cancel",
    ) -> asyncio.Task[R | None]:
        _validate_delay(delay)

        async def run() -> R | None:
            await asyncio.sleep(delay)
            return await _invoke(callback) if callback is not None else None

        return self.ctx.spawn(run(), policy=policy)

    @overload
    def interval(
        self, delay: float, *, immediate: bool = False, policy: TaskPolicy = "cancel"
    ) -> TimerTicks: ...

    @overload
    def interval(
        self,
        delay: float,
        callback: Callable[[], object],
        *,
        immediate: bool = False,
        policy: TaskPolicy = "cancel",
    ) -> asyncio.Task[None]: ...

    def interval(
        self,
        delay: float,
        callback: Callable[[], object] | None = None,
        *,
        immediate: bool = False,
        policy: TaskPolicy = "cancel",
    ) -> asyncio.Task[None] | TimerTicks:
        _validate_delay(delay)
        if callback is None:
            return TimerTicks(self.ctx, delay, immediate=immediate)

        execute = callback

        async def run() -> None:
            if immediate:
                await _invoke(execute)
            while True:
                await asyncio.sleep(delay)
                await _invoke(execute)

        return self.ctx.spawn(run(), policy=policy)

    def debounce(
        self, delay: float, callback: Callable[P, R | Awaitable[R]]
    ) -> Debounced[P, R]:
        return Debounced(self, delay, callback)

    def throttle(
        self,
        delay: float,
        callback: Callable[P, R | Awaitable[R]],
        *,
        trailing: bool = True,
    ) -> Throttled[P, R]:
        return Throttled(self, delay, callback, trailing=trailing)


class TimerTicks:
    """A lifecycle-owned tick stream; use async with or aclose() to stop early."""

    def __init__(self, ctx: Context, delay: float, *, immediate: bool = False) -> None:
        self._closed = False
        self._disposed = False
        self._waiters: list[asyncio.Future[None]] = []

        async def run() -> None:
            if not immediate:
                await asyncio.sleep(delay)
            while True:
                while self._waiters:
                    waiter = self._waiters.pop(0)
                    if not waiter.done():
                        waiter.set_result(None)
                        break
                await asyncio.sleep(delay)

        self._task = ctx.spawn(run(), policy="cancel")
        self._task.add_done_callback(self._stopped)

    def _stopped(self, task: asyncio.Task[None]) -> None:
        if not self._closed:
            self._disposed = True
            self._closed = True
        for waiter in self._waiters:
            if not waiter.done():
                if self._disposed:
                    waiter.set_exception(
                        InactiveContextError("interval context was disposed")
                    )
                else:
                    waiter.set_exception(StopAsyncIteration())
        self._waiters.clear()

    def __aiter__(self) -> Self:
        return self

    async def __anext__(self) -> None:
        if self._disposed:
            raise InactiveContextError("interval context was disposed")
        if self._closed:
            raise StopAsyncIteration
        waiter = asyncio.get_running_loop().create_future()
        self._waiters.append(waiter)
        try:
            await waiter
        finally:
            if waiter in self._waiters:
                self._waiters.remove(waiter)

    async def aclose(self) -> None:
        self._closed = True
        self._task.cancel()
        await asyncio.gather(self._task, return_exceptions=True)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.aclose()
