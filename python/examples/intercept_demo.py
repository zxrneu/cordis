"""Adapt a service explicitly without modifying its original object."""

from __future__ import annotations

import asyncio
from typing import Protocol

from cordis import Context, ServiceKey


class Logger(Protocol):
    def info(self, message: str) -> None: ...


class BaseLogger:
    def info(self, message: str) -> None:
        print(f"INFO: {message}")


class TaggedLogger:
    def __init__(self, base: Logger, tag: str) -> None:
        self.base = base
        self.tag = tag

    def info(self, message: str) -> None:
        self.base.info(f"[{self.tag}] {message}")


LOGGER = ServiceKey[Logger]("example.logger")


def adapt_logger(service: Logger, metadata: object, _ctx: Context) -> Logger:
    if isinstance(metadata, dict) and isinstance(tag := metadata.get("tag"), str):
        return TaggedLogger(service, tag)
    return service


async def main() -> None:
    async with Context() as root:
        root.provide(LOGGER, BaseLogger(), adapt=adapt_logger)
        root.service(LOGGER).info("系统初始化完成")
        user = root.intercept(LOGGER, {"tag": "USER"}).service(LOGGER)
        order = root.intercept(LOGGER, {"tag": "ORDER"}).service(LOGGER)
        user.info("用户登录成功")
        order.info("订单创建完成")


if __name__ == "__main__":
    asyncio.run(main())
