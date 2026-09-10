from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Protocol

from cordis import Context, ServiceKey, plugin


class Greeter(Protocol):
    def __call__(self, name: str, /) -> str: ...


GREETER = ServiceKey[Greeter]("greeter")


@dataclass(frozen=True)
class GreeterConfig:
    prefix: str
    suffix: str


class AppContext(Context):
    """服务描述符同时提供类型提示与运行时依赖检查。"""

    greeter = GREETER


@plugin(provide=[GREETER])
def greeter(ctx: AppContext, config: GreeterConfig) -> None:
    def greet(name: str) -> str:
        return f"{config.prefix} {name} {config.suffix}"

    ctx.provide(GREETER, greet)


@plugin(inject=[GREETER])
def app(ctx: AppContext, _config: None) -> None:
    print(ctx.greeter("Cordis"))


async def main() -> None:
    async with AppContext() as root:
        root.use(app)  # 依赖尚未满足，暂时 PENDING
        root.use(greeter, GreeterConfig(prefix="你好", suffix="世界"))
        await root.flush()


if __name__ == "__main__":
    asyncio.run(main())
