from __future__ import annotations

import functools
import inspect
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextvars import ContextVar
from typing import Any, Generic, TypeVar, cast, overload

from .core import Context, EffectHandle
from .keys import ServiceKey


ConfigT = TypeVar("ConfigT")
ServiceT = TypeVar("ServiceT")

_caller_context: ContextVar[Context | None] = ContextVar(
    "cordis_service_caller", default=None
)


@overload
def current_context() -> Context | None: ...


@overload
def current_context(default: Context) -> Context: ...


def current_context(default: Context | None = None) -> Context | None:
    """Return the Context of the currently executing bound service call."""

    return _caller_context.get() or default


async def _await_in_context(value: Awaitable[Any], ctx: Context) -> Any:
    token = _caller_context.set(ctx)
    try:
        return await value
    finally:
        _caller_context.reset(token)


async def _iterate_async_in_context(
    value: AsyncIterator[Any], ctx: Context
) -> AsyncIterator[Any]:
    token = _caller_context.set(ctx)
    try:
        async for item in value:
            yield item
    finally:
        _caller_context.reset(token)


def _iterate_in_context(value: Iterator[Any], ctx: Context) -> Iterator[Any]:
    token = _caller_context.set(ctx)
    try:
        yield from value
    finally:
        _caller_context.reset(token)


def _contextualize(value: Any, ctx: Context) -> Any:
    if inspect.isawaitable(value):
        return _await_in_context(value, ctx)
    if isinstance(value, AsyncIterator):
        return _iterate_async_in_context(value, ctx)
    if isinstance(value, Iterator):
        return _iterate_in_context(value, ctx)
    return value


def _bind_callable(callback: Callable[..., Any], ctx: Context) -> Callable[..., Any]:
    @functools.wraps(callback)
    def invoke(*args: object, **kwargs: object) -> Any:
        token = _caller_context.set(ctx)
        try:
            result = callback(*args, **kwargs)
        finally:
            _caller_context.reset(token)
        return _contextualize(result, ctx)

    return invoke


class BoundService(Generic[ServiceT]):
    """A lightweight use-site view over a service instance."""

    __slots__ = ("_target", "_context", "_metadata")
    _target: ServiceT
    _context: Context
    _metadata: object

    def __init__(self, target: ServiceT, context: Context, metadata: object) -> None:
        object.__setattr__(self, "_target", target)
        object.__setattr__(self, "_context", context)
        object.__setattr__(self, "_metadata", metadata)

    @property
    def context(self) -> Context:
        return self._context

    @property
    def metadata(self) -> object:
        return self._metadata

    def __getattr__(self, name: str) -> Any:
        token = _caller_context.set(self._context)
        try:
            value = getattr(self._target, name)
        finally:
            _caller_context.reset(token)
        return _bind_callable(value, self._context) if callable(value) else value

    def __setattr__(self, name: str, value: object) -> None:
        token = _caller_context.set(self._context)
        try:
            setattr(self._target, name, value)
        finally:
            _caller_context.reset(token)

    def __call__(self, *args: object, **kwargs: object) -> Any:
        target = cast(Callable[..., Any], self._target)
        return _bind_callable(target, self._context)(*args, **kwargs)


class Service(Generic[ConfigT]):
    """Optional base class for self-registering, context-aware services."""

    def __init__(self, ctx: Context, key: str | ServiceKey[Any]) -> None:
        self._provider_context = ctx
        self._service_key = key
        self._registration: EffectHandle = ctx.provide(key, self)

    @property
    def ctx(self) -> Context:
        ctx = current_context(self._provider_context)
        return self._provider_context if ctx is None else ctx

    @property
    def provider_context(self) -> Context:
        return self._provider_context

    @property
    def service_key(self) -> str | ServiceKey[Any]:
        return self._service_key

    def __cordis_intercept__(
        self, metadata: object, ctx: Context
    ) -> BoundService[Service[ConfigT]]:
        return BoundService(self, ctx, metadata)
