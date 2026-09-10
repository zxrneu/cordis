"""Each E marker must produce a mypy error, checked by tests/test_typing.py."""

import asyncio

from cordis import Context, Fiber
from contracts import (
    COUNT,
    GREETER,
    AppContext,
    GreeterConfig,
    Greeting,
    consumer,
    greeter,
    validated,
)


async def invalid(ctx: AppContext, fiber: Fiber[GreeterConfig, AppContext]) -> None:
    ctx.provide(COUNT, "one")  # E
    ctx.set(COUNT, "two")  # E
    ctx[GREETER].greet(123)  # E
    ctx.service(GREETER).missing()  # E
    ctx.greeter.greet(123)  # E
    _text: str = ctx[COUNT]  # E
    ctx.use(greeter, 123)  # E
    ctx.use(greeter)  # E
    await fiber.update(123)  # E
    Context().use(consumer)  # E
    Context().use(validated, 123)  # E
    await ctx.effect(lambda: 123)  # E
    _task: asyncio.Task[str] = ctx.spawn(asyncio.sleep(0, result=123))  # E
    ctx.spawn(asyncio.sleep(0), policy="forget")  # E
    ctx.spawn(asyncio.sleep(0), timeout="soon")  # E
    ctx.greeter = Greeting("hi")  # E
    ctx.once("ready", lambda: None, task_policy="forget")  # E
