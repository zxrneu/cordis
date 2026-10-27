"""Compile-only examples: every assert_type must hold without casts or Any."""

import asyncio
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from typing import Protocol, TypedDict, assert_type

from cordis import (
    BoundComponent,
    Component,
    ComponentRegistry,
    Context,
    EffectHandle,
    Entry,
    BoundService,
    ContextAware,
    EffectInfo,
    EventKey,
    Fiber,
    FiberView,
    Loader,
    QueryKey,
    ReadinessError,
    ReadinessIssue,
    ShutdownTimeoutError,
    ShutdownBlocker,
    ServiceKey,
    TimerTicks,
    plugin,
)


async def runtime_limits() -> None:
    async with Context(max_tasks=64, error_history_limit=128) as ctx:
        assert_type(ctx.task_count, int)
        assert_type(ctx.error_count, int)
        assert_type(ctx.clear_errors(), None)
        try:
            assert_type(await ctx.wait_ready(timeout=5), None)
        except ReadinessError as error:
            assert_type(error.issues, tuple[ReadinessIssue, ...])
            assert_type(error.cycles, tuple[tuple[str, ...], ...])
        try:
            assert_type(await ctx.close(timeout=None), None)
        except ShutdownTimeoutError as error:
            assert_type(error.blockers, tuple[ShutdownBlocker, ...])


class Greeter(Protocol):
    def greet(self, name: str) -> str: ...


GREETER = ServiceKey[Greeter]("greeter")
COUNT = ServiceKey[int]("count")
MESSAGE = EventKey[str]("message")
PROGRESS = EventKey[int, str]("progress")
FIND_USER = QueryKey[int, str]("find_user")
ALLOWED = QueryKey[int, bool]("allowed")


def handle_message(message: str) -> None:
    pass


async def handle_message_async(message: str) -> None:
    pass


def find_user(user_id: int) -> str | None:
    return str(user_id)


async def find_user_async(user_id: int) -> str | None:
    return str(user_id)


class AppContext(Context):
    greeter = GREETER


class GreetingObject(ContextAware):
    pass


class GreeterConfig(TypedDict):
    prefix: str


@dataclass
class Greeting:
    prefix: str

    def greet(self, name: str) -> str:
        return f"{self.prefix} {name}"


@plugin(provide=[GREETER])
def greeter(ctx: AppContext, config: GreeterConfig) -> None:
    ctx.provide(GREETER, Greeting(config["prefix"]))


@plugin(inject=[GREETER])
def consumer(ctx: AppContext, _config: None) -> None:
    assert_type(ctx.greeter, Greeter)
    assert_type(ctx[GREETER], Greeter)
    assert_type(ctx.service(GREETER), Greeter)
    assert_type(ctx.greeter.greet("Cordis"), str)


def validate(config: GreeterConfig) -> GreeterConfig:
    return config


def decode_config(raw: object) -> GreeterConfig:
    if not isinstance(raw, str):
        raise ValueError("expected a prefix string")
    return {"prefix": raw}


class GreeterSchema:
    def validate_python(self, raw: object) -> GreeterConfig:
        return decode_config(raw)


async def decode_config_async(raw: object) -> GreeterConfig:
    return decode_config(raw)


@plugin(validate=validate)
def validated(ctx: Context, config: GreeterConfig) -> None:
    pass


async def validate_async(config: GreeterConfig) -> GreeterConfig:
    return config


@plugin(validate=validate_async)
async def async_validated(ctx: Context, config: GreeterConfig) -> None:
    pass


async def update(ctx: Context, before: GreeterConfig, after: GreeterConfig) -> None:
    pass


@plugin(update=update, validate=validate)
def updatable(ctx: Context, config: GreeterConfig) -> None:
    pass


class ClassLifecycle:
    def __init__(self, ctx: Context, config: GreeterConfig) -> None:
        self.config = config

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass


ClassPlugin = plugin(ClassLifecycle)


class ObjectPlugin:
    def apply(self, ctx: Context, config: GreeterConfig) -> None:
        pass


@plugin
def generator_plugin(
    ctx: Context, config: GreeterConfig
) -> Iterator[Callable[[], None]]:
    yield lambda: None


@plugin
async def async_generator_plugin(
    ctx: Context, config: GreeterConfig
) -> AsyncIterator[Callable[[], None]]:
    yield lambda: None


@contextmanager
def resource() -> Iterator[None]:
    yield


@asynccontextmanager
async def async_resource() -> AsyncIterator[None]:
    yield


@contextmanager
def numbered_resource() -> Iterator[int]:
    yield 42


@asynccontextmanager
async def async_numbered_resource() -> AsyncIterator[int]:
    yield 42


async def verify() -> None:
    assert_type(greeter, Component[GreeterConfig, AppContext])
    assert_type(consumer, Component[None, AppContext])
    assert_type(validated, Component[GreeterConfig, Context])
    assert_type(async_validated, Component[GreeterConfig, Context])
    assert_type(updatable, Component[GreeterConfig, Context])
    assert_type(ClassPlugin, Component[GreeterConfig, Context])
    assert_type(generator_plugin, Component[GreeterConfig, Context])
    assert_type(async_generator_plugin, Component[GreeterConfig, Context])
    assert_type(AppContext.greeter, ServiceKey[Greeter])

    async with AppContext() as ctx:
        assert_type(ctx, AppContext)
        assert_type(ctx.enter_context(numbered_resource()), int)
        assert_type(await ctx.enter_async_context(async_numbered_resource()), int)
        async with ctx.scope() as scoped:
            assert_type(scoped, AppContext)
            assert_type(scoped.service(COUNT), int)
        config: GreeterConfig = {"prefix": "hello"}
        fiber = ctx.use(greeter, config)
        assert_type(fiber, Fiber[GreeterConfig, AppContext])
        assert_type(fiber.config, GreeterConfig)
        assert_type(fiber.ctx, AppContext)
        assert_type(await fiber, Fiber[GreeterConfig, AppContext])
        assert_type(await fiber.wait(), Fiber[GreeterConfig, AppContext])
        await fiber.update({"prefix": "hi"})
        assert_type(ctx.use(consumer), Fiber[None, AppContext])
        assert_type(ctx.include(consumer), Fiber[None, AppContext])
        assert_type(ctx.isolate(GREETER), AppContext)
        assert_type(ctx.intercept(GREETER, {"prefix": "hi"}), AppContext)
        assert_type(ctx.get(COUNT), int | None)
        assert_type(ctx.get(COUNT, 0), int)
        assert_type(ctx.get(COUNT, "missing"), int | str)
        assert_type(ctx.get(COUNT, None), int | None)
        assert_type(ctx.get(COUNT, include_inactive=True), int | None)
        assert_type(ctx.provide(COUNT, 1), EffectHandle)
        ctx.set(COUNT, 2)
        assert_type(ctx.spawn(asyncio.sleep(0, result=42)), asyncio.Task[int])
        ctx.on(PROGRESS, lambda count, label: None)
        ctx.emit(PROGRESS, 1, "ready")
        await ctx.parallel(PROGRESS, 2, "done")
        assert_type(await ctx.serial(PROGRESS, 3, "step"), object)
        assert_type(ctx.bail(PROGRESS, 4, "step"), object)
        assert_type(ctx.fiber, FiberView[AppContext])
        assert_type(await ctx.fiber, FiberView[AppContext])
        assert_type(await ctx.fiber.wait(), FiberView[AppContext])
        assert_type(ctx.fiber.ctx, AppContext)
        assert_type(ctx.fiber.ctx[COUNT], int)
        assert_type(ctx.fiber.config, object)
        assert_type(ctx.fiber.tasks, tuple[asyncio.Task[object], ...])
        assert_type(ctx.fiber.effects, tuple[EffectInfo, ...])
        assert_type(ctx.extend(port=8080), AppContext)
        assert_type(ctx.timer.timeout(0.1), asyncio.Task[None])
        assert_type(ctx.timer.timeout(0.1, lambda: 42), asyncio.Task[int])
        assert_type(ctx.timer.interval(0.1), TimerTicks)
        assert_type(ctx.timer.interval(0.1, lambda: None), asyncio.Task[None])
        assert_type(
            ctx.timer.throttle(0.1, find_user, trailing=False)(1),
            asyncio.Task[str | None],
        )
        assert_type(
            GreetingObject(ctx).__cordis_intercept__(None, ctx),
            BoundService[GreetingObject],
        )
        assert_type(ctx.fibers, tuple[FiberView[Context], ...])
        assert_type(ctx.registry, ComponentRegistry[AppContext])
        assert_type(ctx.errors, tuple[tuple[Exception, FiberView[Context] | None], ...])
        for mounted in ctx.fibers:
            assert_type(mounted.ctx, Context)
            assert_type(mounted.ctx[COUNT], int)
        loader = Loader(ctx)
        assert_type(loader.provide(), EffectHandle)
        loaded = loader.get("greeter")
        assert_type(loaded, FiberView[AppContext] | None)
        if loaded is not None:
            assert_type(loaded.ctx, AppContext)
            assert_type(loaded.ctx[COUNT], int)
        assert_type(await loader.reload("greeter"), FiberView[AppContext])
        binding = greeter.configure(config)
        assert_type(binding, BoundComponent[GreeterConfig, AppContext])
        assert_type(
            await greeter.decode("hi", decode_config),
            BoundComponent[GreeterConfig, AppContext],
        )
        assert_type(
            await greeter.decode("hi", decode_config_async),
            BoundComponent[GreeterConfig, AppContext],
        )
        assert_type(
            await greeter.decode_schema("hi", GreeterSchema()),
            BoundComponent[GreeterConfig, AppContext],
        )
        mounted_typed = await loader.mount("bound", binding)
        assert_type(mounted_typed, Fiber[GreeterConfig, AppContext])
        await mounted_typed.update(config)
        assert_type(
            await loader.mount("base", validated.configure(config)),
            Fiber[GreeterConfig, AppContext],
        )
        await loader.unmount("bound")
        entry = Entry("greeter", greeter, config, intercept={"count": 1})
        assert_type(entry.intercept["count"], object)
        metadata = entry.intercept["count"]
        if isinstance(metadata, int):
            assert_type(metadata.bit_length(), int)
        answer = ctx.bail("answer")
        assert_type(answer, object)
        if isinstance(answer, int):
            assert_type(answer.bit_length(), int)
        assert_type(await ctx.serial("answer"), object)
        assert_type(ctx.waterfall("answer", final=lambda: 1), object)
        assert_type(ctx.on(MESSAGE, handle_message), EffectHandle)
        assert_type(ctx.on(MESSAGE, handle_message_async), EffectHandle)
        assert_type(ctx.once(MESSAGE, handle_message), EffectHandle)
        ctx.emit(MESSAGE, "hello")
        await ctx.parallel(MESSAGE, "hello")
        assert_type(ctx.handle(FIND_USER, find_user), EffectHandle)
        assert_type(ctx.handle(FIND_USER, find_user_async), EffectHandle)
        assert_type(await ctx.query(FIND_USER, 1), str | None)
        ctx.handle(ALLOWED, lambda user_id: False)
        assert_type(await ctx.query(ALLOWED, 1), bool | None)
        ctx.provide("numeric", 1, adapt=lambda value, metadata, ctx: value + 1)
        assert_type(await ctx.effect(resource), EffectHandle)
        assert_type(await ctx.effect(async_resource), EffectHandle)

        def temporary(ctx: AppContext, config: GreeterConfig) -> None:
            pass

        assert_type(ctx.use(temporary, config), Fiber[GreeterConfig, AppContext])
        assert_type(
            ctx.inject([GREETER], temporary, config), Fiber[GreeterConfig, AppContext]
        )
        assert_type(
            Context().use(ObjectPlugin(), config), Fiber[GreeterConfig, Context]
        )
