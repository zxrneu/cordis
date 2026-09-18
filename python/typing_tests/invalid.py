"""Each E marker must produce a mypy error, checked by tests/test_typing.py."""

import asyncio

from cordis import BoundComponent, Context, Entry, Fiber, FiberState, Loader
from contracts import (
    COUNT,
    MESSAGE,
    PROGRESS,
    FIND_USER,
    GREETER,
    AppContext,
    GreeterConfig,
    Greeting,
    consumer,
    greeter,
    validated,
    handle_message,
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
    ctx.fiber.ctx.provide(COUNT, "wrong")  # E
    ctx.fiber.ctx.set(COUNT, "wrong")  # E
    ctx.fiber.ctx[GREETER].greet(123)  # E
    ctx.fiber.config.bit_length()  # E
    for mounted in ctx.fibers:
        mounted.ctx.provide(COUNT, "wrong")  # E
    for _error, owner in ctx.errors:
        if owner is not None:
            owner.ctx.set(COUNT, "wrong")  # E
    loader = Loader(ctx)
    loaded = loader.get("greeter")
    if loaded is not None:
        loaded.ctx.provide(COUNT, "wrong")  # E
    reloaded = await loader.reload("greeter")
    reloaded.ctx.set(COUNT, "wrong")  # E
    ctx.bail("answer").bit_length()  # E
    (await ctx.serial("answer")).bit_length()  # E
    ctx.waterfall("answer", final=lambda: "text").bit_length()  # E
    entry = Entry("consumer", consumer, intercept={"count": "text"})
    entry.intercept["count"].bit_length()  # E
    ctx.provide("numeric", 1, adapt=lambda value, metadata, ctx: value.missing())  # E
    ctx.provide("numeric", 1, adapt=lambda value, metadata, ctx: metadata.missing())  # E
    for task in fiber.tasks:
        (await task).bit_length()  # E
    await ctx.fiber.update(123)  # E
    await (await ctx.fiber).update(123)  # E
    await (await ctx.fiber.wait()).update(123)  # E
    ctx.fiber.config = 123  # E
    ctx.fiber.state = FiberState.ACTIVE  # E
    ctx.fiber.ctx = Context()  # E
    ctx.fiber.component.apply(ctx, 123)  # E
    fiber.ctx = Context()  # E
    loader.ctx = Context()  # E
    for mounted in ctx.fibers:
        await mounted.update(123)  # E
    for _error, owner in ctx.errors:
        if owner is not None:
            await owner.update(123)  # E
    if loaded is not None:
        await loaded.update(123)  # E
    await reloaded.update(123)  # E
    greeter.configure(123)  # E
    BoundComponent(greeter, 123)  # E
    await greeter.decode({}, lambda raw: 123)  # E
    await greeter.decode({}, lambda raw: raw)  # E
    config: GreeterConfig = {"prefix": "hello"}
    binding = greeter.configure(config)
    binding.config = config  # E
    await Loader(Context()).mount("bad", binding)  # E
    typed_mount = await loader.mount("good", binding)
    await typed_mount.update(123)  # E
    ctx.emit(MESSAGE, 123)  # E
    ctx.emit(MESSAGE)  # E
    ctx.emit(MESSAGE, "hi", "extra")  # E
    ctx.emit(PROGRESS, "one", "ready")  # E
    ctx.emit(PROGRESS, 1)  # E
    await ctx.parallel(MESSAGE, 123)  # E
    ctx.on(MESSAGE, lambda message: 123)  # E
    ctx.on(MESSAGE, lambda message: message.bit_length())  # E
    ctx.once(MESSAGE, lambda message: 123)  # E
    ctx.on(FIND_USER, handle_message)  # E
    await ctx.serial(MESSAGE, "hi")  # E
    ctx.handle(FIND_USER, lambda user_id: 123)  # E
    ctx.handle(FIND_USER, handle_message)  # E
    await ctx.query(FIND_USER, "wrong")  # E
    await ctx.query(MESSAGE, "hi")  # E
    _wrong_result: int | None = await ctx.query(FIND_USER, 1)  # E
