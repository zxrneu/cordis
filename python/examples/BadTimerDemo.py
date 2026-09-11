import asyncio
from cordis import Context, plugin

class BadTimer:
    def __init__(self, ctx: Context) -> None:
        self.ctx = ctx  # 绑定提供方的 ctx

    def interval(self, seconds: float, callback):
        print(f"[启动任务] 任务实际挂载在 Fiber: {self.ctx.fiber.name}")
        async def _loop():
            while True:
                await asyncio.sleep(seconds)
                callback()
        return self.ctx.spawn(_loop(), policy="cancel")

@plugin(provide=["timer"])
def timer_plugin(ctx: Context, _config: None):
    # ctx.provide("timer", BadTimer(ctx))  # 未使用 adapt
    # 使用adapt
    def adapt(value: BadTimer, metadata: None, ctx: Context) -> BadTimer:
        return BadTimer(ctx)
    ctx.provide("timer", BadTimer(ctx), adapt=adapt)

@plugin(inject=["timer"])
def consumer_plugin(ctx: Context, _config: None):
    print(f"[消费者] 消费者的 Fiber 是: {ctx.fiber.name}")
    ctx.timer.interval(0.2, lambda: print("  -> 定时器还在跳动..."))

async def main():
    async with Context() as root:
        root.use(timer_plugin)
        consumer = root.use(consumer_plugin)
        await root.flush()
        await asyncio.sleep(0.3)

        print("\n>>> 卸载 consumer_plugin <<<")
        await consumer.dispose()
        print(">>> 卸载完成 <<<\n")

        print("等待观察：")
        await asyncio.sleep(3)

asyncio.run(main())
