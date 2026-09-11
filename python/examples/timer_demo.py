import asyncio
import inspect
from collections.abc import Callable
from typing import Any
from cordis import Context, plugin


# =====================================================================
# 方式一：直接在插件中使用 ctx.spawn 启动受管循环 (推荐：最简单直接)
# =====================================================================
@plugin
def simple_ticker_plugin(ctx: Context, _config: None) -> None:
    print(f"[simple_ticker_plugin] 初始化，id: {id(ctx)}")
    async def loop() -> None:
        count = 0
        try:
            while True:
                await asyncio.sleep(0.3)
                count += 1
                print(f"[简单定时] 第 {count} 次心跳")
        finally:
            print("[简单定时] 插件卸载或任务取消，安全退出")

    # policy="cancel"：当所属插件或上下文卸载时，自动取消该任务并等待 finally 完成
    ctx.spawn(loop(), policy="cancel", name="simple:ticker")


# =====================================================================
# 方式二：封装成通用的 Timer 服务 (通过 adapt 绑定调用方生命周期)
# =====================================================================
class TimerService:
    def __init__(self, ctx: Context) -> None:
        self.ctx = ctx

    def interval(
        self, seconds: float, callback: Callable[[], Any]
    ) -> asyncio.Task[None]:
        async def _loop() -> None:
            try:
                while True:
                    await asyncio.sleep(seconds)
                    res = callback()
                    if inspect.isawaitable(res):
                        await res
            except asyncio.CancelledError:
                pass

        # 任务挂载在当前调用者的 context 上，生命周期与调用方完全同步
        return self.ctx.spawn(_loop(), policy="cancel", name="timer:interval")

    def timeout(
        self, seconds: float, callback: Callable[[], Any]
    ) -> asyncio.Task[None]:
        async def _run() -> None:
            try:
                await asyncio.sleep(seconds)
                res = callback()
                if inspect.isawaitable(res):
                    await res
            except asyncio.CancelledError:
                pass

        return self.ctx.spawn(_run(), policy="cancel", name="timer:timeout")


@plugin(provide=["timer"])
def timer_plugin(ctx: Context, _config: None) -> None:
    print(f"[Timer Plugin] 初始化，id: {id(ctx)}")
    # 使用 adapt 将服务实例与调用方的上下文绑定
    ctx.provide(
        "timer",
        TimerService(ctx),
        adapt=lambda _service, _meta, caller_ctx: TimerService(caller_ctx),
    )


@plugin(inject=["timer"])
def consumer_plugin(ctx: Context, _config: None) -> None:
    ctx.timer.interval(0.2, lambda: print("[Timer 服务] 收到周期性回调"))


# =====================================================================
# 测试运行
# =====================================================================
async def main() -> None:
    async with Context() as root:
        print(f"root id: {id(root)}")
        print("--- 演示 1: 直接使用 ctx.spawn ---")
        fiber1 = root.use(simple_ticker_plugin)
        await asyncio.sleep(0.7)
        print("卸载 simple_ticker_plugin...")
        await fiber1.dispose()

        print("\n--- 演示 2: 使用 Timer 服务与生命周期绑定 ---")
        root.use(timer_plugin)
        fiber2 = root.use(consumer_plugin)
        await root.flush()

        await asyncio.sleep(0.5)
        print("卸载 consumer_plugin (Timer 任务应自动停止)...")
        await fiber2.dispose()

        print("等待 0.4 秒验证没有多余的回调输出...")
        await asyncio.sleep(0.4)
        print("测试完成")


if __name__ == "__main__":
    asyncio.run(main())
