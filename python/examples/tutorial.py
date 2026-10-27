"""Run with: uv run python -m examples.tutorial"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Protocol

from cordis import Context, FiberState, ServiceKey, plugin


class Greeter(Protocol):
    def __call__(self, name: str, /) -> str: ...


GREETER = ServiceKey[Greeter]("greeter")


class AppContext(Context):
    """Retain the context subclass for compatibility; new code can use Context."""

    greeter = GREETER


@dataclass(frozen=True)
class GreeterConfig:
    prefix: str


@plugin(provide=[GREETER])
def greeter(ctx: Context, config: GreeterConfig) -> None:
    def greet(name: str) -> str:
        return f"{config.prefix} {name}"

    ctx.provide(GREETER, greet)
    print(f"服务启动: {config.prefix}")

    def cleanup() -> None:
        print(f"服务清理: {config.prefix}")

    ctx.defer(cleanup)


@plugin(inject=[GREETER])
def app(ctx: Context, _config: None) -> None:
    greet = ctx.service(GREETER)

    async def on_greet(name: str) -> str:
        await asyncio.sleep(0)
        message = greet(name)
        print(message)
        return message

    ctx.on("greet", on_greet, timeout=5)
    print("应用启动")

    def cleanup() -> None:
        print("应用清理")

    ctx.defer(cleanup)


async def main() -> None:
    async with Context() as root:
        consumer = root.use(app)
        assert (await consumer).state is FiberState.PENDING
        print(f"等待依赖: {consumer.state.value}")

        provider = root.use(greeter, GreeterConfig(prefix="你好"))
        await root.flush()
        await provider
        assert (await consumer).state is FiberState.ACTIVE
        await root.parallel("greet", "Cordis")

        print("-- 更新配置 --")
        await provider.update(GreeterConfig(prefix="Hello"))
        await root.flush()
        assert (await consumer).state is FiberState.ACTIVE
        await root.parallel("greet", "Cordis")

        print("-- 移除服务 --")
        await provider.dispose()
        await root.flush()
        assert (await consumer).state is FiberState.PENDING
        print(f"等待依赖: {consumer.state.value}")


if __name__ == "__main__":
    asyncio.run(main())
