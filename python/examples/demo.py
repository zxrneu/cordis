from __future__ import annotations

import asyncio
from typing import Any

from cordis import Context, plugin


@plugin(provide=["greeter"])
def greeter(ctx: Context, config: dict[str, str]) -> None:
    ctx.provide("greeter", lambda name: f"{config['prefix']} {name} {config['suffix']}")


@plugin(inject=["greeter"])
def app(ctx: Context, _config: Any) -> None:
    print(ctx.greeter("Cordis"))


async def main() -> None:
    async with Context() as root:
        root.use(app)  # 依赖尚未满足，暂时 PENDING
        root.use(greeter, {"prefix": "你好", "suffix": "世界"})
        await root.flush()


asyncio.run(main())