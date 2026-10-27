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
    """Keep descriptor access available for existing callers."""

    greeter = GREETER


@plugin(provide=[GREETER])
def greeter(ctx: Context, config: GreeterConfig) -> None:
    def greet(name: str) -> str:
        return f"{config.prefix} {name} {config.suffix}"

    ctx.provide(GREETER, greet)


@plugin(inject=[GREETER])
def app(ctx: Context, _config: None) -> None:
    greet = ctx.service(GREETER)
    print(greet("Cordis"))


async def main() -> None:
    async with Context() as root:
        root.use(app)  # 依赖尚未满足，暂时 PENDING
        root.use(greeter, GreeterConfig(prefix="你好", suffix="世界"))
        await root.flush()


if __name__ == "__main__":
    asyncio.run(main())
