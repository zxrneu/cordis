"""Compile-only examples: every assert_type must hold without casts or Any."""

import asyncio
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from typing import Protocol, TypedDict, assert_type

from cordis import Component, Context, EffectHandle, Fiber, ServiceKey, plugin


class Greeter(Protocol):
    def greet(self, name: str) -> str: ...


GREETER = ServiceKey[Greeter]("greeter")
COUNT = ServiceKey[int]("count")


class AppContext(Context):
    greeter = GREETER


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
        config: GreeterConfig = {"prefix": "hello"}
        fiber = ctx.use(greeter, config)
        assert_type(fiber, Fiber[GreeterConfig, AppContext])
        assert_type(fiber.config, GreeterConfig)
        assert_type(fiber.ctx, AppContext)
        assert_type(await fiber, Fiber[GreeterConfig, AppContext])
        assert_type(await fiber.wait(), Fiber[GreeterConfig, AppContext])
        await fiber.update({"prefix": "hi"})
        assert_type(ctx.use(consumer), Fiber[None, AppContext])
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
