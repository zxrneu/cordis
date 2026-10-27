"""Show how an explicit owner prevents tasks from outliving their consumer."""

import asyncio

from cordis import Context
from examples.timer_demo import TIMER, TimerService


async def main() -> None:
    async with Context() as root:
        root.provide(TIMER, TimerService())
        async with root.scope() as consumer:
            timer = consumer.service(TIMER)
            task = timer.interval(consumer, 0.2, lambda: print("消费者的周期回调"))
            await asyncio.sleep(0.3)
        print(f"消费者退出后任务已取消：{task.cancelled()}")


if __name__ == "__main__":
    asyncio.run(main())
