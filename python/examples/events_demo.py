import asyncio
from typing import Any
from cordis import Context


async def main() -> None:
    ctx = Context()

    # -------------------------------------------------------------
    # 1. 广播 (Fire-and-Forget)：ctx.emit
    # 常规通知；同步监听器立即执行，异步监听器作为托管任务调度，不阻塞当前流程
    # -------------------------------------------------------------
    ctx.on("notify", lambda msg: print(f"[1. 广播] 同步接收: {msg}"))

    async def async_notify(msg: str) -> None:
        await asyncio.sleep(0.01)
        print(f"[1. 广播] 异步后台任务接收: {msg}")

    ctx.on("notify", async_notify)
    ctx.emit("notify", "系统初始化")
    await ctx.flush()  # 等待托管后台任务执行完毕

    # -------------------------------------------------------------
    # 2. 并发聚合 (Fan-out/Fan-in)：await ctx.parallel
    # 并行执行所有监听器并等待全部完成；如有异常将聚合成 ExceptionGroup 抛出
    # -------------------------------------------------------------
    async def init_cache() -> None:
        await asyncio.sleep(0.01)
        print("[2. 并行] 缓存服务初始化完成")

    async def init_database() -> None:
        await asyncio.sleep(0.01)
        print("[2. 并行] 数据库连接池初始化完成")

    ctx.on("service/start", init_cache)
    ctx.on("service/start", init_database)
    await ctx.parallel("service/start")

    # -------------------------------------------------------------
    # 3. 串行短路 (Sequential Fallback)：await ctx.serial
    # 顺序执行监听器，首个返回非 None / 非 False 的监听器即刻截断并作为结果返回
    # (注：同步场景下可使用 ctx.bail，已在 bail_demo.py 中演示)
    # -------------------------------------------------------------
    async def verify_amount(order: dict[str, Any]) -> str | None:
        if order["amount"] > 1000:
            return "金额超限 (Rule 1 命中)"
        return None

    async def verify_user(order: dict[str, Any]) -> str | None:
        print("[3. 串行] 此处不应被执行")
        return "用户黑名单 (Rule 2 命中)"

    ctx.on("order/verify", verify_amount)
    ctx.on("order/verify", verify_user)
    reason = await ctx.serial("order/verify", {"amount": 5000, "user": "normal"})
    print(f"[3. 串行短路结果]: {reason}")

    # -------------------------------------------------------------
    # 4. 流水线/洋葱圈 (Pipeline)：ctx.waterfall
    # 类似 Express/Koa 中间件链，每个监听器接收 next 闭包并传递修改后的数据
    # -------------------------------------------------------------
    def add_meta(data: dict[str, Any], next_: Any) -> Any:
        data["tag"] = "SECURITY"
        print("[4. 流水线] 中间件 1: 附加安全标签")
        return next_(data)

    def sign_payload(data: dict[str, Any], next_: Any) -> Any:
        data["signed"] = True
        print("[4. 流水线] 中间件 2: 数据签名")
        return next_(data)

    ctx.on("request/pipeline", add_meta)
    ctx.on("request/pipeline", sign_payload)
    result = ctx.waterfall(
        "request/pipeline",
        {"id": 1001},
        final=lambda payload: payload,
    )
    print(f"[4. 流水线最终数据]: {result}")


if __name__ == "__main__":
    asyncio.run(main())
