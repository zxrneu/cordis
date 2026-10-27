"""Manage periodic work with explicit ownership."""

from __future__ import annotations

import asyncio
from collections.abc import Callable

from cordis import Context, ServiceKey, plugin


class TimerService:
    """Schedule callbacks for an explicitly supplied owner."""

    def interval(
        self, owner: Context, seconds: float, callback: Callable[[], None]
    ) -> asyncio.Task[None]:
        async def loop() -> None:
            while True:
                await asyncio.sleep(seconds)
                callback()

        return owner.spawn(loop(), policy="cancel", name="example:interval")


TIMER = ServiceKey[TimerService]("example.timer")


@plugin(provide=[TIMER])
def timer_plugin(ctx: Context, _config: None) -> None:
    ctx.provide(TIMER, TimerService())


@plugin(inject=[TIMER])
def consumer_plugin(ctx: Context, _config: None) -> None:
    timer = ctx.service(TIMER)
    timer.interval(ctx, 0.2, lambda: print("周期回调"))


async def main() -> None:
    async with Context() as root:
        await root.use(timer_plugin)
        consumer = await root.use(consumer_plugin)
        await asyncio.sleep(0.5)
        await consumer.dispose()
        print("消费者已卸载，周期任务已经取消。")

        # Built-in timers already belong to the context they were obtained from.
        async with root.scope() as scope:
            await scope.timer.timeout(0.1, lambda: print("局部作用域的一次性回调"))


if __name__ == "__main__":
    asyncio.run(main())
