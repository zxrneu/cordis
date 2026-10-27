import asyncio

from cordis import Context


async def main() -> None:
    async with Context() as ctx:
        # 1. 注册监听
        ctx.on("order/created", lambda order_id: print(f"通知: 订单 {order_id} 创建"))

        # 2. 短路鉴定示例 (Bail)
        ctx.on(
            "order/verify", lambda order: "金额超限" if order["amount"] > 1000 else None
        )
        ctx.on(
            "order/verify",
            lambda order: "用户被封禁" if order["user"] == "black" else None,
        )

        reason = ctx.bail("order/verify", {"amount": 5000, "user": "normal"})
        print("校验拦截:", reason)  # 输出: 金额超限


if __name__ == "__main__":
    asyncio.run(main())
