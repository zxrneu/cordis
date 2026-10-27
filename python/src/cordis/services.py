from __future__ import annotations

import functools
import inspect
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator, Mapping
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Generic, Self, TypeVar, cast, overload

from .core import Context, EffectHandle, _service_name
from .keys import ServiceKey

ConfigT = TypeVar("ConfigT")
ServiceT = TypeVar("ServiceT")


@dataclass(frozen=True, slots=True)
class _Call:
    caller: Context
    definition: Context


_call: ContextVar[_Call | None] = ContextVar("cordis_service_call", default=None)


@overload
def current_context() -> Context | None: ...


@overload
def current_context(default: Context) -> Context: ...


def current_context(default: Context | None = None) -> Context | None:
    """Return the resource owner of the current service call."""
    call = _call.get()
    return call.caller if call is not None else default


def definition_context(ctx: Context) -> Context:
    """Resolve dependencies at the definition site, effects at the call site."""
    call = _call.get()
    if (
        call is not None
        and ctx._fiber is call.caller._fiber
        and ctx._is_descendant_of(call.caller)
    ):
        return call.definition
    return ctx


async def _await_in_context(
    value: Awaitable[Any], call: _Call, bound: Any = None
) -> Any:
    token = _call.set(call)
    try:
        return _contextualize(await value, call, bound)
    finally:
        _call.reset(token)


async def _iterate_async_in_context(
    value: AsyncIterator[Any], call: _Call
) -> AsyncIterator[Any]:
    try:
        while True:
            token = _call.set(call)
            try:
                item = await anext(value)
            except StopAsyncIteration:
                return
            finally:
                _call.reset(token)
            yield _contextualize(item, call)
    finally:
        close = getattr(value, "aclose", None)
        if close is not None:
            token = _call.set(call)
            try:
                await close()
            finally:
                _call.reset(token)


def _iterate_in_context(value: Iterator[Any], call: _Call) -> Iterator[Any]:
    try:
        while True:
            token = _call.set(call)
            try:
                item = next(value)
            except StopIteration:
                return
            finally:
                _call.reset(token)
            yield _contextualize(item, call)
    finally:
        close = getattr(value, "close", None)
        if close is not None:
            token = _call.set(call)
            try:
                close()
            finally:
                _call.reset(token)


def _contextualize(value: Any, call: _Call, bound: Any = None) -> Any:
    if inspect.isawaitable(value):
        return _await_in_context(value, call, bound)
    if isinstance(value, AsyncIterator):
        return _iterate_async_in_context(value, call)
    if isinstance(value, Iterator):
        return _iterate_in_context(value, call)
    if bound is not None and value is getattr(bound, "_target", None):
        return bound
    if isinstance(value, ContextAware):
        return value.__cordis_intercept__(None, call.caller)
    return value


def _bind_callable(
    callback: Callable[..., Any], call: _Call, bound: Any = None
) -> Callable[..., Any]:
    @functools.wraps(callback)
    def invoke(*args: object, **kwargs: object) -> Any:
        token = _call.set(call)
        try:
            result = callback(*args, **kwargs)
        finally:
            _call.reset(token)
        return _contextualize(result, call, bound)

    return invoke


class BoundService(Generic[ServiceT]):
    """Provide legacy caller propagation for context-aware service objects."""

    __slots__ = ("_target", "_context", "_metadata", "_call", "_access")
    _target: ServiceT
    _context: Context
    _metadata: object
    _call: _Call
    _access: _Call

    def __init__(self, target: ServiceT, context: Context, metadata: object) -> None:
        object.__setattr__(self, "_target", target)
        object.__setattr__(self, "_context", context)
        object.__setattr__(self, "_metadata", metadata)
        object.__setattr__(self, "_access", _Call(context, definition_context(context)))
        definition = (
            target.provider_context
            if isinstance(target, ContextAware) and target.preserve_dependencies
            else context
        )
        object.__setattr__(self, "_call", _Call(context, definition))

    @property
    def context(self) -> Context:
        return self._context

    @property
    def definition_context(self) -> Context:
        return self._call.definition

    @property
    def metadata(self) -> object:
        return self._metadata

    def _associated_name(self, name: str) -> str | None:
        namespace = getattr(self._target, "namespace", None)
        if namespace is None:
            return None
        key = f"{namespace}.{name}"
        runtime = self._context._runtime
        return (
            key
            if key in runtime.accessors
            or any(s.name == key for s in runtime.services.values())
            else None
        )

    def __getattr__(self, name: str) -> Any:
        associated = self._associated_name(name)
        token = _call.set(self._access if associated is not None else self._call)
        try:
            if associated is not None:
                accessor = self._context._runtime.accessors.get(associated)
                if accessor is not None:
                    return (
                        accessor.get_for(self._context, self)
                        if accessor.get_for
                        else accessor.get(self._context)
                    )
                return self._context.service(associated)
            value = getattr(self._target, name)
        finally:
            _call.reset(token)
        return (
            _bind_callable(value, self._call, bound=self)
            if callable(value)
            else _contextualize(value, self._call)
        )

    def __setattr__(self, name: str, value: object) -> None:
        associated = self._associated_name(name)
        token = _call.set(self._access if associated is not None else self._call)
        try:
            if associated is not None:
                accessor = self._context._runtime.accessors.get(associated)
                if accessor is not None and accessor.set_for is not None:
                    accessor.set_for(self._context, self, value)
                else:
                    setattr(self._context, associated, value)
                return
            setattr(self._target, name, value)
        finally:
            _call.reset(token)

    def __call__(self, *args: object, **kwargs: object) -> Any:
        return _bind_callable(
            cast(Callable[..., Any], self._target), self._call, bound=self
        )(*args, **kwargs)


class ContextAware:
    """Opt into legacy caller propagation and optional association.

    Set preserve_dependencies=False for infrastructure that deliberately works
    entirely in the caller's dependency scope. Prefer plain objects with
    explicit dependency and owner parameters for new application services.
    """

    def __init__(
        self,
        ctx: Context,
        *,
        namespace: str | None = None,
        preserve_dependencies: bool = True,
    ) -> None:
        self._provider_context = definition_context(ctx)
        self.namespace = namespace
        self.preserve_dependencies = preserve_dependencies

    @property
    def ctx(self) -> Context:
        return current_context(self._provider_context)

    @property
    def provider_context(self) -> Context:
        return self._provider_context

    def __cordis_intercept__(
        self, metadata: object, ctx: Context
    ) -> BoundService[Self]:
        return BoundService(self, ctx, metadata)


class Service(ContextAware, Generic[ConfigT]):
    """A self-registering context-aware object with overridable availability."""

    def __init__(
        self,
        ctx: Context,
        key: str | ServiceKey[Any],
        *,
        preserve_dependencies: bool = True,
    ) -> None:
        super().__init__(
            ctx,
            namespace=_service_name(key),
            preserve_dependencies=preserve_dependencies,
        )
        self._service_key = key
        self._registration: EffectHandle = ctx.provide(
            key,
            self,
            check=lambda caller: bool(
                self.__cordis_intercept__(caller.metadata(key), caller).check()
            ),
        )

    @property
    def service_key(self) -> str | ServiceKey[Any]:
        return self._service_key

    def check(self) -> bool:
        """Override to gate activation; call ctx.notify(key) after a change."""
        return True

    def resolve_config(
        self,
        base: Mapping[str, object] | None = None,
        head: Mapping[str, object] | None = None,
        *,
        merge: Callable[..., ConfigT] | None = None,
    ) -> ConfigT:
        layers = self.ctx._intercepts.layers_for(_service_name(self._service_key))
        configs = [
            base or {},
            *(layer for layer in reversed(layers) if isinstance(layer, Mapping)),
            head or {},
        ]
        if merge is not None:
            return merge(*configs)
        result: dict[str, object] = {}
        for config in configs:
            result.update(config)
        return cast(ConfigT, result)
