from __future__ import annotations

import asyncio
import contextvars
import inspect
import math
from collections import defaultdict
from collections.abc import (
    AsyncIterable,
    AsyncIterator,
    Awaitable,
    Callable,
    Generator,
    Iterable,
    Iterator,
    Mapping,
    MutableSequence,
)
from contextlib import AbstractAsyncContextManager, AbstractContextManager
from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType, TracebackType
from typing import (
    Any,
    Generic,
    Literal,
    Protocol,
    Self,
    TypeAlias,
    TypeVar,
    cast,
    overload,
)

from .keys import EventKey, QueryKey, RequestT, ResultT, ServiceKey


Cleanup: TypeAlias = Callable[[], object]
ConfigT = TypeVar("ConfigT")
ContextT = TypeVar("ContextT", bound="Context")
ContextCov = TypeVar("ContextCov", bound="Context", covariant=True)
ContextContra = TypeVar("ContextContra", bound="Context", contravariant=True)
ConfigContra = TypeVar("ConfigContra", contravariant=True)
EffectValue: TypeAlias = (
    "None | Cleanup | EffectHandle | Iterable[Cleanup | EffectHandle | None] "
    "| AsyncIterable[Cleanup | EffectHandle | None] "
    "| AbstractContextManager[object] | AbstractAsyncContextManager[object]"
)
EffectResult: TypeAlias = "EffectValue | Awaitable[EffectValue]"
EffectCallback: TypeAlias = Callable[[], EffectResult]


class SupportsStart(Protocol):
    def start(self) -> EffectResult: ...


class SupportsStop(Protocol):
    def stop(self) -> object: ...


class SupportsClose(Protocol):
    def close(self) -> object: ...


PluginResult: TypeAlias = "EffectResult | SupportsStart | SupportsStop | SupportsClose"
PluginCallback: TypeAlias = Callable[[ContextT, ConfigT], PluginResult]


class Plugin(Protocol[ContextContra, ConfigContra]):
    def apply(self, ctx: ContextContra, config: ConfigContra, /) -> PluginResult: ...


# Heterogeneous dependency declarations erase the value type; reads and writes
# retain it through the ServiceKey overloads on Context.
ServiceName: TypeAlias = str | ServiceKey[Any]
Injection: TypeAlias = Iterable[ServiceName] | Mapping[ServiceName, object]
TaskPolicy: TypeAlias = Literal["drain", "cancel"]
EventScope: TypeAlias = "Context | Callable[[Context], bool] | None"
# Only the internal heterogeneous registries erase event contracts. String
# events and typed key objects have separate identities at runtime.
_EventName: TypeAlias = str | EventKey[Any]
_DispatchName: TypeAlias = _EventName | QueryKey[Any, Any]
T = TypeVar("T")
D = TypeVar("D")
_EMPTY = object()


class CordisError(RuntimeError):
    pass


class InactiveContextError(CordisError):
    pass


class InactiveServiceError(CordisError):
    pass


class UndeclaredServiceError(CordisError):
    pass


class DuplicateServiceError(CordisError):
    pass


class DuplicateQueryError(CordisError):
    pass


class MissingProvisionError(CordisError):
    pass


class FiberState(str, Enum):
    PENDING = "pending"
    LOADING = "loading"
    ACTIVE = "active"
    FAILED = "failed"
    UNLOADING = "unloading"
    DISPOSED = "disposed"


class FiberView(Protocol[ContextCov]):
    """Read-only inspection and config-independent lifecycle operations.

    This is a static view of the same Fiber, not a runtime wrapper or sandbox.
    Keep the Fiber returned by use()/mount() to update a known config type.
    """

    @property
    def ctx(self) -> ContextCov: ...

    @property
    def config(self) -> object: ...

    @property
    def component(self) -> object: ...

    @property
    def uid(self) -> int | None: ...

    @property
    def name(self) -> str: ...

    @property
    def state(self) -> FiberState: ...

    @property
    def inject(self) -> Mapping[str, object]: ...

    @property
    def error(self) -> Exception | None: ...

    @property
    def cleanup_error(self) -> ExceptionGroup | None: ...

    @property
    def tasks(self) -> tuple[asyncio.Task[object], ...]: ...

    async def dispose(self) -> None: ...

    async def restart(self) -> None: ...

    async def wait(self, *, raise_error: bool = True) -> Self: ...

    def __await__(self) -> Generator[object, None, Self]: ...


def _service_name(value: ServiceName) -> str:
    return value.name if isinstance(value, ServiceKey) else value


def _normalize_inject(value: Injection | None) -> Mapping[str, object]:
    if value is None:
        return MappingProxyType({})
    if isinstance(value, Mapping):
        return MappingProxyType(
            {_service_name(key): metadata for key, metadata in value.items()}
        )
    return MappingProxyType(dict.fromkeys(map(_service_name, value)))


@dataclass(frozen=True, init=False)
class Component(Generic[ConfigT, ContextContra]):
    apply: PluginCallback[ContextContra, ConfigT]
    inject: Mapping[str, object]
    provide: frozenset[str]
    name: str | None = None
    validate: Callable[[ConfigT], ConfigT | Awaitable[ConfigT]] | None = None
    update: Callable[[ContextContra, ConfigT, ConfigT], object] | None = None

    def __init__(
        self,
        apply: PluginCallback[ContextContra, ConfigT],
        inject: Injection = (),
        provide: Iterable[ServiceName] = (),
        name: str | None = None,
        validate: Callable[[ConfigT], ConfigT | Awaitable[ConfigT]] | None = None,
        update: Callable[[ContextContra, ConfigT, ConfigT], object] | None = None,
    ) -> None:
        object.__setattr__(self, "apply", apply)
        object.__setattr__(self, "inject", _normalize_inject(inject))
        object.__setattr__(self, "provide", frozenset(map(_service_name, provide)))
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "validate", validate)
        object.__setattr__(self, "update", update)

    def configure(self, config: ConfigT) -> BoundComponent[ConfigT, ContextContra]:
        """Bind a typed configuration before entering a heterogeneous loader."""
        return BoundComponent(self, config)

    async def decode(
        self,
        raw: object,
        decoder: Callable[[object], ConfigT | Awaitable[ConfigT]],
    ) -> BoundComponent[ConfigT, ContextContra]:
        """Decode external data before binding; validate still runs on activation."""
        return self.configure(await _await(decoder(raw)))


@dataclass(frozen=True)
class BoundComponent(Generic[ConfigT, ContextContra]):
    component: Component[ConfigT, ContextContra]
    config: ConfigT

    def _mount(
        self: BoundComponent[ConfigT, ContextT], ctx: ContextT
    ) -> Fiber[ConfigT, ContextT]:
        return ctx.use(self.component, self.config)


class _PluginDecorator(Protocol):
    def __call__(
        self, callback: PluginCallback[ContextT, ConfigT], /
    ) -> Component[ConfigT, ContextT]: ...


class _ConfiguredDecorator(Protocol[ConfigT]):
    def __call__(
        self, callback: PluginCallback[ContextT, ConfigT], /
    ) -> Component[ConfigT, ContextT]: ...


@overload
def plugin(
    target: PluginCallback[ContextT, ConfigT],
    *,
    inject: Injection = (),
    provide: Iterable[ServiceName] = (),
    name: str | None = None,
    validate: Callable[[ConfigT], ConfigT | Awaitable[ConfigT]] | None = None,
    update: Callable[[ContextT, ConfigT, ConfigT], object] | None = None,
) -> Component[ConfigT, ContextT]: ...


@overload
def plugin(
    target: None = None,
    *,
    inject: Injection = (),
    provide: Iterable[ServiceName] = (),
    name: str | None = None,
    validate: None = None,
    update: None = None,
) -> _PluginDecorator: ...


@overload
def plugin(
    target: None = None,
    *,
    inject: Injection = (),
    provide: Iterable[ServiceName] = (),
    name: str | None = None,
    validate: Callable[[ConfigT], ConfigT | Awaitable[ConfigT]],
    update: None = None,
) -> _ConfiguredDecorator[ConfigT]: ...


@overload
def plugin(
    target: None = None,
    *,
    inject: Injection = (),
    provide: Iterable[ServiceName] = (),
    name: str | None = None,
    validate: Callable[[ConfigT], ConfigT | Awaitable[ConfigT]] | None = None,
    update: Callable[[ContextT, ConfigT, ConfigT], object],
) -> Callable[[PluginCallback[ContextT, ConfigT]], Component[ConfigT, ContextT]]: ...


def plugin(
    target: PluginCallback[Any, Any] | None = None,
    *,
    inject: Injection = (),
    provide: Iterable[ServiceName] = (),
    name: str | None = None,
    validate: Callable[[Any], Any] | None = None,
    update: Callable[[Any, Any, Any], object] | None = None,
) -> Component[Any, Any] | Callable[[PluginCallback[Any, Any]], Component[Any, Any]]:
    """Declare a function or class as a Cordis component."""

    def wrap(callback: PluginCallback[Any, Any]) -> Component[Any, Any]:
        return Component(
            callback,
            _normalize_inject(inject),
            frozenset(provide),
            name or getattr(callback, "__name__", type(callback).__name__),
            validate,
            update,
        )

    return wrap(target) if target is not None else wrap


component = plugin


@dataclass(slots=True)
class _Service:
    name: str
    value: object
    fiber: "Fiber[Any, Any]"
    realm: object
    check: Callable[["Context"], bool] | None = None
    adapt: Callable[[object, object, "Context"], object] | None = None


@dataclass(slots=True)
class _Listener:
    ctx: "Context"
    callback: Callable[..., object]
    sink: "_Sink"
    global_: bool = False
    task_policy: TaskPolicy = "drain"
    timeout: float | None = None


@dataclass(slots=True)
class _Sink:
    fiber: "Fiber[Any, Any]"
    token: object
    actions: MutableSequence[Cleanup | _Effect]
    parent: "_Sink | None" = None
    closed: bool = False
    tasks: dict[asyncio.Task[object], "_TaskOptions"] = field(default_factory=dict)

    @property
    def open(self) -> bool:
        return not self.closed and (self.parent is None or self.parent.open)


@dataclass(slots=True)
class _TaskOptions:
    policy: TaskPolicy
    timeout: float | None
    stopping: asyncio.Task[None] | None = None


def _validate_task_options(policy: TaskPolicy, timeout: float | None) -> None:
    if policy not in ("drain", "cancel"):
        raise ValueError("task policy must be 'drain' or 'cancel'")
    if timeout is not None and (not math.isfinite(timeout) or timeout < 0):
        raise ValueError("task timeout must be a finite, non-negative number")


def _close_coroutine(value: object) -> None:
    if inspect.iscoroutine(value):
        value.close()


def _effect_error(error: Exception | asyncio.CancelledError, label: str) -> Exception:
    if isinstance(error, Exception):
        return error
    failure = CordisError(f"{label} was cancelled")
    failure.__cause__ = error
    return failure


async def _finish_task(task: asyncio.Task[object], options: _TaskOptions) -> None:
    if not task.done():
        if options.policy == "cancel":
            task.cancel()
        elif options.timeout is not None:
            _, pending = await asyncio.wait((task,), timeout=options.timeout)
            if pending:
                task.cancel()
    # Cancellation is cooperative: wait for the coroutine's finally blocks before
    # releasing services it may still be using. Task failures are reported once
    # by the completion callback, independently of teardown.
    await asyncio.gather(task, return_exceptions=True)


async def _stop_tasks(sink: _Sink) -> None:
    tasks: dict[asyncio.Task[object], _TaskOptions] = {}

    def collect(scope: _Sink) -> None:
        scope.closed = True
        tasks.update(scope.tasks)
        for action in scope.actions:
            if isinstance(action, _Effect):
                collect(action.sink)

    collect(sink)
    if tasks:
        stopping = []
        for task, options in tasks.items():
            if options.stopping is None:
                options.stopping = asyncio.create_task(_finish_task(task, options))
            stopping.append(options.stopping)
        await asyncio.gather(*(asyncio.shield(task) for task in stopping))


_collector: contextvars.ContextVar[_Sink | None] = contextvars.ContextVar(
    "cordis_effect_collector", default=None
)


async def _await(value: T | Awaitable[T]) -> T:
    return cast(T, await value) if inspect.isawaitable(value) else value


async def _settle(awaitable: Awaitable[T], *, name: str) -> T:
    """Finish a mutation, including its recovery, before propagating cancellation."""

    async def run() -> T:
        return await awaitable

    task = asyncio.create_task(run(), name=name)
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError as cancelled:
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        try:
            task.result()
        except BaseException as error:
            raise cancelled from error
        raise


async def _dispose_actions(sink: _Sink, label: str) -> None:
    # All work in this effect tree must settle before any resources are undone,
    # including resources registered after a background task was started.
    await _stop_tasks(sink)
    actions = sink.actions
    errors: list[Exception] = []
    while actions:
        action = actions.pop()
        try:
            if isinstance(action, _Effect):
                await action.dispose()
            else:
                await _await(action())
        except (Exception, asyncio.CancelledError) as error:
            errors.append(_effect_error(error, f"cleanup in {label}"))
    if errors:
        raise ExceptionGroup(f"errors while disposing {label}", errors)


def _collect_value(value: object, sink: _Sink) -> None:
    if value is None:
        return
    if isinstance(value, EffectHandle):
        value._effect.attach(sink)
        return
    if callable(value):
        sink.actions.append(value)
        return
    raise TypeError("an effect must return or yield cleanup callables")


async def _execute(
    callback: Callable[[], object], guard: Callable[[], bool], sink: _Sink
) -> None:
    token = _collector.set(sink)
    iterator: Iterator[object] | AsyncIterator[object] | None = None
    exhausted = False
    failure: Exception | asyncio.CancelledError | None = None
    try:
        value = callback()
        if inspect.isawaitable(value) and not isinstance(value, AsyncIterable):
            value = await value

        if hasattr(value, "__aenter__") and hasattr(value, "__aexit__"):
            await value.__aenter__()

            async def close_async_context() -> None:
                await value.__aexit__(None, None, None)

            sink.actions.append(close_async_context)
            return
        if hasattr(value, "__enter__") and hasattr(value, "__exit__"):
            value.__enter__()
            sink.actions.append(lambda: value.__exit__(None, None, None))
            return

        if isinstance(value, AsyncIterable):
            iterator = value.__aiter__()
            while guard():
                try:
                    item = await anext(iterator)
                except StopAsyncIteration:
                    exhausted = True
                    break
                _collect_value(item, sink)
            return

        if isinstance(value, Iterable) and not isinstance(value, (str, bytes, Mapping)):
            iterator = iter(value)
            while guard():
                try:
                    item = next(iterator)
                except StopIteration:
                    exhausted = True
                    break
                _collect_value(item, sink)
            return

        # Factories may return the same lifecycle protocols as class plugins.
        # Register stop before start so a partial startup can still roll back.
        if not callable(value):
            start = getattr(value, "start", None)
            stop = getattr(value, "stop", None) or getattr(value, "close", None)
            if callable(start) or callable(stop):
                if callable(stop):
                    sink.actions.append(stop)
                if callable(start):
                    await _execute(start, guard, sink)
                return

        _collect_value(value, sink)
    except (Exception, asyncio.CancelledError) as error:
        failure = error
        raise
    finally:
        try:
            if iterator is not None and not exhausted:
                close = getattr(iterator, "aclose", None) or getattr(
                    iterator, "close", None
                )
                if close:
                    try:
                        await _await(close())
                    except (Exception, asyncio.CancelledError) as close_error:
                        if failure is not None:
                            raise ExceptionGroup(
                                "effect initialization and iterator closing failed",
                                [
                                    _effect_error(failure, "effect initialization"),
                                    _effect_error(close_error, "iterator closing"),
                                ],
                            ) from failure
                        raise
        finally:
            _collector.reset(token)


class EffectHandle:
    """An idempotent cleanup handle returned by context operations."""

    def __init__(self, effect: "_Effect") -> None:
        self._effect = effect

    async def __call__(self) -> None:
        await self._effect.dispose()


class _Effect:
    def __init__(self, fiber: "Fiber[Any, Any]", label: str) -> None:
        self.fiber = fiber
        self.label = label
        self.actions: list[Cleanup | _Effect] = []
        self.sink = _Sink(fiber, fiber._load_token, self.actions)
        self.armed = True
        self.parent: _Sink | None = None
        self.task: asyncio.Task[None] | None = None
        self._disposal: asyncio.Task[None] | None = None
        self.error: Exception | None = None
        self.handle = EffectHandle(self)
        self.before_dispose: Callable[[], None] | None = None

    def attach(self, parent: _Sink) -> None:
        if self.parent is parent:
            return
        if self.parent is not None and self in self.parent.actions:
            self.parent.actions.remove(self)
        self.parent = parent
        self.sink.parent = parent
        parent.actions.append(self)

    @classmethod
    def completed(
        cls, fiber: "Fiber[Any, Any]", cleanups: Iterable[Cleanup], label: str
    ) -> "_Effect":
        effect = cls(fiber, label)
        effect.actions.extend(cleanups)
        return effect

    def start(self, callback: Callable[[], object]) -> None:
        self.task = asyncio.create_task(
            self._run(callback, self.sink), name=f"cordis:effect:{self.label}"
        )

    async def _run(self, callback: Callable[[], object], sink: _Sink) -> None:
        try:
            await _execute(callback, lambda: self.armed, sink)
        except (Exception, asyncio.CancelledError) as exception:
            error = _effect_error(exception, f"effect {self.label!r}")
            self.armed = False
            try:
                await _dispose_actions(self.sink, self.label)
            except ExceptionGroup as cleanup_error:
                error = ExceptionGroup(
                    f"effect {self.label!r} failed and rollback also failed",
                    [error, cleanup_error],
                )
            self.error = error
            self.detach()
            raise error

    def detach(self) -> None:
        if self.parent is not None and self in self.parent.actions:
            self.parent.actions.remove(self)
        self.parent = None

    async def wait(self) -> EffectHandle:
        if self.task:
            await asyncio.shield(self.task)
        return self.handle

    async def dispose(self) -> None:
        self._assert_can_dispose()
        if self._disposal is None:
            self._disposal = asyncio.create_task(self._dispose())
        await asyncio.shield(self._disposal)

    def _assert_can_dispose(self) -> None:
        if self._disposal is not None and self._disposal.done():
            return
        if asyncio.current_task() in self.fiber._tasks:
            scope = _collector.get()
            while scope is not None:
                if scope is self.sink:
                    raise InactiveContextError(
                        "a managed task cannot dispose its own effect scope"
                    )
                scope = scope.parent
        if self.before_dispose is not None:
            self.before_dispose()
        # Run guards in the requesting task, before spawning a cleanup task
        # which would otherwise hide the managed caller's identity.
        for action in self.actions:
            if isinstance(action, _Effect):
                action._assert_can_dispose()

    async def _dispose(self) -> None:
        if (
            not self.armed
            and not self.actions
            and not self.sink.tasks
            and (self.task is None or self.task.done())
        ):
            self.detach()
            return
        self.armed = False
        await _stop_tasks(self.sink)
        if self.task:
            try:
                await asyncio.shield(self.task)
            except Exception:
                self.detach()
                return
        try:
            await _dispose_actions(self.sink, self.label)
        finally:
            self.detach()


class Fiber(Generic[ConfigT, ContextT]):
    def __init__(
        self,
        runtime: "_Runtime",
        parent: ContextT,
        component: Component[ConfigT, ContextT] | None,
        config: ConfigT,
        *,
        root: bool = False,
    ) -> None:
        self.runtime = runtime
        self.parent = parent
        self.component = component
        self.config = config
        self.uid: int | None = 0 if root else runtime.next_uid()
        self.name = (
            "root" if component is None else component.name or f"component-{self.uid}"
        )
        self.state = FiberState.ACTIVE if root else FiberState.PENDING
        self.desired = True
        self.revision = 0
        self.target: tuple[int, tuple[int, ...]] | None | object = _EMPTY
        self.committed: dict[str, _Service] = {}
        self.committed_realms: dict[str, object] = {}
        self._committed_contexts: dict[Context, dict[str, object]] = {}
        self._parent_effect: _Effect | None = None
        self.inertia: asyncio.Task[None] | None = None
        self.error: Exception | None = None
        self.cleanup_error: ExceptionGroup | None = None
        self._actions: list[Cleanup | _Effect] = []
        self._load_token = object()
        self._scope = _Sink(self, self._load_token, self._actions)
        self._tasks: set[asyncio.Task[object]] = set()
        intercepts = {
            key: metadata
            for key, metadata in (component.inject.items() if component else ())
            if metadata is not None
        }
        self._ctx = (
            parent if root else parent._derive(fiber=self, intercepts=intercepts)
        )

    @property
    def ctx(self) -> ContextT:
        return self._ctx

    @property
    def inject(self) -> Mapping[str, object]:
        return self.component.inject if self.component else MappingProxyType({})

    @property
    def tasks(self) -> tuple[asyncio.Task[object], ...]:
        """Snapshot of this fiber's unfinished managed tasks."""
        return tuple(task for task in self._tasks if not task.done())

    def _assert_effects_allowed(self) -> None:
        if self.state not in (FiberState.LOADING, FiberState.ACTIVE):
            raise InactiveContextError(
                f"cannot create effect in {self.state.value} component {self.name!r}"
            )
        self._sink()

    def _sink(self) -> _Sink:
        current = _collector.get()
        sink = current if current is not None and current.fiber is self else self._scope
        if sink.token is not self._load_token or not sink.open:
            raise InactiveContextError(
                f"stale or closing scope attempted an effect in component {self.name!r}"
            )
        return sink

    def _attach_effect(self, effect: _Effect) -> EffectHandle:
        self._assert_effects_allowed()
        effect.attach(self._sink())
        return effect.handle

    def _compute_target(self) -> tuple[int, tuple[int, ...]] | None:
        if not self.desired or self.state is FiberState.FAILED:
            return None
        if self.parent._fiber is not self.runtime.root._fiber:
            if self.parent._fiber.state is not FiberState.ACTIVE:
                return None
        providers: list[int] = []
        for name in self.inject:
            service = self.runtime.resolve(self.ctx, name)
            if service is None or service.fiber.uid is None:
                return None
            providers.append(service.fiber.uid)
        return self.revision, tuple(providers)

    def refresh(self) -> None:
        target = self._compute_target()
        if target == self.target:
            return
        self.target = target
        if self.inertia is not None:
            return
        if self.state in (FiberState.PENDING, FiberState.FAILED):
            if target is not None:
                self._set_state(FiberState.LOADING)
                self._spawn_drive()
        elif self.state is FiberState.ACTIVE:
            self._begin_unload()

    def _set_state(self, state: FiberState) -> None:
        old = self.state
        self.state = state
        if old is not state:
            self.runtime.emit("internal/status", self, old)

    def _spawn_drive(self) -> None:
        self.inertia = asyncio.create_task(
            self._drive(), name=f"cordis:fiber:{self.name}:{self.uid}"
        )

    def _begin_unload(self) -> None:
        self._set_state(FiberState.UNLOADING)
        self.runtime.provider_leaving(self)
        self._spawn_drive()

    async def _validate_config(self, config: ConfigT) -> ConfigT:
        if self.component is None or self.component.validate is None:
            return config
        return await _await(self.component.validate(config))

    def _component_callback(self) -> Callable[[], object]:
        assert self.component is not None
        callback = self.component.apply

        async def invoke_class() -> object:
            instance = callback(self.ctx, self.config)
            stop = getattr(instance, "stop", None) or getattr(instance, "close", None)
            if stop:
                self._actions.append(stop)
            start = getattr(instance, "start", None)
            return await _await(start()) if start else None

        if inspect.isclass(callback):
            return invoke_class
        return lambda: callback(self.ctx, self.config)

    def _commit(self) -> None:
        committed: dict[str, _Service] = {}
        realms: dict[str, object] = {}
        for name in self.inject:
            service = self.runtime.resolve(self.ctx, name)
            if service is None:
                raise RuntimeError(
                    f"dependency {name!r} disappeared before loading {self.name!r}"
                )
            committed[name] = service
            realms[name] = self.ctx._realm(name)
        self.committed = committed
        self.committed_realms = realms
        context: Context | None = self.ctx
        self._committed_contexts = {}
        while context is not None:
            self._committed_contexts[context] = dict(context._realms_local)
            context = context._parent

    def _committed_realm(self, ctx: Context, name: str) -> object:
        context: Context | None = ctx
        while context is not None:
            realms = self._committed_contexts.get(context, context._realms_local)
            if name in realms:
                return realms[name]
            context = context._parent
        return ctx._realm(name)

    def _check_provides(self) -> None:
        assert self.component is not None
        for name in self.component.provide:
            service = self.runtime.services.get(self.ctx._realm(name))
            if service is None or service.fiber is not self:
                raise MissingProvisionError(
                    f"component {self.name!r} declared but did not provide service {name!r}"
                )

    async def _recover(self) -> None:
        try:
            await _dispose_actions(self._scope, f"component:{self.name}")
        except ExceptionGroup as error:
            self.cleanup_error = error
            self.runtime.report(error, self)

    async def _drain_dependents(self) -> None:
        dependents = self.runtime.dependents_of(self)
        if dependents:
            await asyncio.gather(
                *(fiber.wait(raise_error=False) for fiber in dependents)
            )

    async def _drive(self) -> None:
        try:
            while True:
                if self.state is FiberState.UNLOADING:
                    await self._drain_dependents()
                    await self._recover()
                    self.committed.clear()
                    self.committed_realms.clear()
                    self._committed_contexts.clear()
                    self._load_token = object()
                    if not self.desired:
                        self._set_state(FiberState.DISPOSED)
                        self.runtime.remove(self)
                        return
                    self.target = self._compute_target()
                    if self.target is None:
                        self._set_state(FiberState.PENDING)
                        return
                    self._set_state(FiberState.LOADING)

                if self.state is not FiberState.LOADING:
                    return

                target = self.target
                self._load_token = object()
                self._scope = _Sink(self, self._load_token, self._actions)
                self.cleanup_error = None
                try:
                    self.config = await self._validate_config(self.config)
                    if self.target != target or not self.desired:
                        self._set_state(FiberState.UNLOADING)
                        self.runtime.provider_leaving(self)
                        continue
                    self._commit()
                    await _execute(
                        self._component_callback(),
                        lambda: self.target == target and self.desired,
                        self._scope,
                    )
                    if self.target == target and self.desired:
                        self._check_provides()
                except (Exception, asyncio.CancelledError) as exception:
                    load_error = _effect_error(exception, f"component {self.name!r}")
                    try:
                        await _dispose_actions(self._scope, f"component:{self.name}")
                    except ExceptionGroup as cleanup_error:
                        load_error = ExceptionGroup(
                            f"component {self.name!r} failed and rollback also failed",
                            [load_error, cleanup_error],
                        )
                    self.committed.clear()
                    self.committed_realms.clear()
                    self._committed_contexts.clear()
                    self.error = load_error
                    self.target = None
                    self._set_state(
                        FiberState.FAILED if self.desired else FiberState.DISPOSED
                    )
                    if not self.desired:
                        self.runtime.remove(self)
                    self.runtime.report(load_error, self)
                    return

                if self.target == target and self.desired:
                    self.error = None
                    self._set_state(FiberState.ACTIVE)
                    self.runtime.provider_active(self)
                    return

                self._set_state(FiberState.UNLOADING)
                self.runtime.provider_leaving(self)
        finally:
            self.inertia = None
            if self.state in (FiberState.PENDING, FiberState.FAILED):
                self.refresh()

    async def dispose(self) -> None:
        self.runtime.assert_can_wait_for(self)
        if self.state is FiberState.DISPOSED:
            if self.cleanup_error:
                raise self.cleanup_error
            return
        self.desired = False
        self.refresh()
        if (
            self.state in (FiberState.PENDING, FiberState.FAILED)
            and self.inertia is None
        ):
            self._set_state(FiberState.DISPOSED)
            self.runtime.remove(self)
        await self.wait(raise_error=False)
        if self.cleanup_error:
            raise self.cleanup_error

    async def restart(self) -> None:
        self.runtime.assert_can_wait_for(self)
        if not self.desired or self.state is FiberState.DISPOSED:
            raise InactiveContextError(
                f"cannot restart disposed component {self.name!r}"
            )
        self.error = None
        self.revision += 1
        if self.state is FiberState.FAILED:
            self._set_state(FiberState.PENDING)
            self.target = _EMPTY
        self.refresh()
        await self.wait()

    async def update(self, config: ConfigT, *, rollback: bool = True) -> None:
        self.runtime.assert_can_wait_for(self)
        if not self.desired or self.state is FiberState.DISPOSED:
            raise InactiveContextError(
                f"cannot update disposed component {self.name!r}"
            )
        await _settle(
            self._update(config, rollback=rollback), name=f"cordis:update:{self.name}"
        )

    async def _update(self, config: ConfigT, *, rollback: bool) -> None:
        config = await self._validate_config(config)
        if config == self.config:
            return
        previous = self.config
        if (
            self.component is not None
            and self.component.update is not None
            and self.state is FiberState.ACTIVE
        ):
            await _await(self.component.update(self.ctx, previous, config))
            self.config = config
            return
        self.config = config
        try:
            await self.restart()
        except Exception:
            if rollback:
                original = self.error
                self.config = previous
                await self.restart()
                if original:
                    raise original
            raise

    async def wait(self, *, raise_error: bool = True) -> Self:
        if self.inertia is not None:
            self.runtime.assert_can_wait_for(self)
        while self.inertia is not None:
            await asyncio.shield(self.inertia)
        if raise_error and self.error:
            raise self.error
        return self

    def __await__(self) -> Generator[object, None, Self]:
        return self.wait().__await__()


class _Runtime:
    def __init__(self) -> None:
        self.uid = 0
        self.fibers: list[Fiber[Any, Any]] = []
        self.services: dict[object, _Service] = {}
        self.listeners: dict[_EventName, list[_Listener]] = defaultdict(list)
        self.queries: dict[QueryKey[Any, Any], _Listener] = {}
        self.errors: list[tuple[Exception, Fiber[Any, Any] | None]] = []
        self.root: Context

    def next_uid(self) -> int:
        self.uid += 1
        return self.uid

    def assert_can_wait_for(self, target: Fiber[Any, Any] | None) -> None:
        current = asyncio.current_task()
        owner = next(
            (
                fiber
                for fiber in [self.root._fiber, *self.fibers]
                if current in fiber._tasks
            ),
            None,
        )
        if owner is None:
            return
        pending = [owner]
        visited: set[Fiber[Any, Any]] = set()
        while pending:
            fiber = pending.pop()
            if fiber in visited:
                continue
            visited.add(fiber)
            if target is None or fiber is target:
                raise InactiveContextError(
                    "a managed task cannot wait for teardown of its own scope "
                    "or a dependency; request the change from the host instead"
                )
            pending.extend(service.fiber for service in fiber.committed.values())
            if fiber is not self.root._fiber:
                pending.append(fiber.parent._fiber)

    def add(self, fiber: Fiber[Any, Any]) -> None:
        self.fibers.append(fiber)

    def remove(self, fiber: Fiber[Any, Any]) -> None:
        if fiber in self.fibers:
            self.fibers.remove(fiber)
        fiber.uid = None
        if fiber._parent_effect is not None:
            # Keep a failed cleanup registered so the parent's later shutdown
            # still reports it. Successful manual disposal releases ownership.
            if fiber.cleanup_error is None:
                fiber._parent_effect.detach()
            fiber._parent_effect = None

    def resolve(
        self, ctx: "Context", name: str, *, active: bool = True
    ) -> _Service | None:
        service = self.services.get(ctx._realm(name))
        if service is None:
            return None
        if active and service.fiber.state is not FiberState.ACTIVE:
            return None
        if service.check:
            try:
                if not service.check(ctx):
                    return None
            except Exception as error:
                self.report(error, service.fiber)
                return None
        return service

    def provided_names(self, fiber: Fiber[Any, Any]) -> set[str]:
        return {
            service.name for service in self.services.values() if service.fiber is fiber
        }

    def notify(self, ctx: "Context", names: Iterable[str]) -> list[Fiber[Any, Any]]:
        names = set(names)
        affected: list[Fiber[Any, Any]] = []
        for fiber in self.fibers.copy():
            if not names.intersection(fiber.inject):
                continue
            if not any(
                fiber.ctx._realm(name) is ctx._realm(name)
                for name in names
                if name in fiber.inject
            ):
                continue
            fiber.refresh()
            affected.append(fiber)
        return affected

    def dependents_of(self, provider: Fiber[Any, Any]) -> list[Fiber[Any, Any]]:
        result: list[Fiber[Any, Any]] = []
        for fiber in self.fibers.copy():
            if fiber is provider:
                continue
            is_child = fiber.parent._fiber is provider
            consumes = any(
                service.fiber is provider for service in fiber.committed.values()
            )
            if (is_child or consumes) and fiber.inertia is not None:
                result.append(fiber)
        return result

    def provider_leaving(self, fiber: Fiber[Any, Any]) -> None:
        names = self.provided_names(fiber)
        if names:
            self.notify(fiber.ctx, names)
        for child in self.fibers.copy():
            if child.parent._fiber is fiber:
                child.refresh()

    def provider_active(self, fiber: Fiber[Any, Any]) -> None:
        names = self.provided_names(fiber)
        if names:
            self.notify(fiber.ctx, names)
        for child in self.fibers.copy():
            if child.parent._fiber is fiber:
                child.refresh()

    def reassign_context_realms(
        self, ctx: "Context", realms: Mapping[str, object]
    ) -> None:
        keys = set(ctx._realms_local) | set(realms)
        old = {key: ctx._realm(key) for key in keys}
        parent = ctx._parent
        if parent is None:
            raise ValueError("cannot reassign root realms")
        new = {
            key: realms[key] if key in realms else parent._realm(key) for key in keys
        }

        moves: list[tuple[object, object, _Service]] = []
        for key in keys:
            if old[key] is new[key]:
                continue
            service = self.services.get(old[key])
            if service is None or not service.fiber.ctx._is_descendant_of(ctx):
                continue
            conflict = self.services.get(new[key])
            if conflict is not None and conflict is not service:
                raise DuplicateServiceError(
                    f"cannot move service {key!r}; target realm is provided by {conflict.fiber.name!r}"
                )
            moves.append((old[key], new[key], service))

        ctx._realms_local.clear()
        ctx._realms_local.update(realms)
        for source, target, service in moves:
            if self.services.get(source) is service:
                del self.services[source]
            self.services[target] = service
            service.realm = target
        for fiber in self.fibers.copy():
            fiber.refresh()

    def report(self, error: Exception, fiber: Fiber[Any, Any] | None = None) -> None:
        self.errors.append((error, fiber))
        if self.listeners.get("internal/error"):
            self.emit("internal/error", error, fiber)

    def _listeners(self, name: _EventName, scope: EventScope = None) -> list[_Listener]:
        listeners: list[_Listener] = []
        for listener in list(self.listeners.get(name, ())):
            if (
                listener.ctx._fiber.state not in (FiberState.LOADING, FiberState.ACTIVE)
                or listener.sink.token is not listener.ctx._fiber._load_token
                or not listener.sink.open
            ):
                continue
            if listener.global_ or scope is None:
                listeners.append(listener)
            elif isinstance(scope, Context) and listener.ctx._is_descendant_of(scope):
                listeners.append(listener)
            elif callable(scope) and scope(listener.ctx):
                listeners.append(listener)
        return listeners

    def _invoke(
        self,
        listener: _Listener,
        name: _DispatchName,
        args: tuple[object, ...],
        *,
        sync: bool = False,
    ) -> object:
        # A callback executes in its registration scope, even when another
        # plugin emits the event. Recheck snapshots after earlier callbacks.
        if (
            listener.ctx._fiber.state not in (FiberState.LOADING, FiberState.ACTIVE)
            or listener.sink.token is not listener.ctx._fiber._load_token
            or not listener.sink.open
        ):
            return None
        token = _collector.set(listener.sink)
        try:
            result = listener.callback(*args)
            if not inspect.isawaitable(result):
                return result
            if sync:
                _close_coroutine(result)
                raise TypeError("bail listeners must be synchronous")
            if (
                isinstance(result, asyncio.Task)
                and result in listener.ctx._fiber._tasks
            ):
                # A synchronous listener may return ctx.spawn(...). Preserve
                # that task's explicit policy and avoid reporting failures twice.
                return result
            return listener.ctx._spawn(
                result,
                policy=listener.task_policy,
                timeout=listener.timeout,
                name=f"cordis:event:{name}",
                source=name if isinstance(name, str) else None,
            )
        finally:
            _collector.reset(token)

    def emit(self, name: _EventName, *args: object, scope: EventScope = None) -> None:
        for listener in self._listeners(name, scope):
            try:
                self._invoke(listener, name, args)
            except Exception as error:
                if name == "internal/error":
                    self.errors.append((error, listener.ctx._fiber))
                else:
                    self.report(error, listener.ctx._fiber)

    async def flush(self) -> None:
        while True:
            tasks = [
                fiber.inertia for fiber in self.fibers if fiber.inertia is not None
            ]
            if not tasks:
                return
            await asyncio.gather(*(asyncio.shield(task) for task in tasks))


class Context:
    """A first-class context mediating all plugin effects and coeffects."""

    _runtime: _Runtime
    _parent: Context | None
    _fiber: Fiber[Any, Self]
    _realms_local: dict[str, object]
    _intercepts_local: dict[str, object]
    _closed: bool
    _close_task: asyncio.Task[None] | None

    def __init__(self) -> None:
        runtime = _Runtime()
        object.__setattr__(self, "_runtime", runtime)
        object.__setattr__(self, "_parent", None)
        object.__setattr__(self, "_realms_local", {})
        object.__setattr__(self, "_intercepts_local", {})
        object.__setattr__(self, "_closed", False)
        object.__setattr__(self, "_close_task", None)
        root = Fiber(runtime, self, None, None, root=True)
        object.__setattr__(self, "_fiber", root)
        runtime.root = self

    def _derive(
        self,
        *,
        fiber: Fiber[Any, Self] | None = None,
        realms: Mapping[str, object] | None = None,
        intercepts: Mapping[str, object] | None = None,
    ) -> Self:
        ctx = object.__new__(type(self))
        object.__setattr__(ctx, "_runtime", self._runtime)
        object.__setattr__(ctx, "_parent", self)
        object.__setattr__(ctx, "_realms_local", dict(realms or {}))
        object.__setattr__(ctx, "_intercepts_local", dict(intercepts or {}))
        object.__setattr__(ctx, "_closed", False)
        object.__setattr__(ctx, "_fiber", fiber or self._fiber)
        return ctx

    def _is_descendant_of(self, other: "Context") -> bool:
        ctx: Context | None = self
        while ctx is not None:
            if ctx is other:
                return True
            ctx = ctx._parent
        return False

    @property
    def fiber(self) -> FiberView[Self]:
        return self._fiber

    @property
    def fibers(self) -> tuple[FiberView[Context], ...]:
        return tuple(self._runtime.fibers)

    @property
    def errors(self) -> tuple[tuple[Exception, FiberView[Context] | None], ...]:
        return tuple(self._runtime.errors)

    def _realm(self, name: str) -> object:
        ctx: Context | None = self
        while ctx is not None:
            if name in ctx._realms_local:
                return ctx._realms_local[name]
            ctx = ctx._parent
        return self._runtime.root._realms_local.setdefault(name, object())

    def isolate(self, *names: ServiceName, realm: object | None = None) -> Self:
        if realm is not None and len(names) != 1:
            raise ValueError("an explicit realm can isolate exactly one service")
        return self._derive(
            realms={_service_name(name): realm or object() for name in names}
        )

    def intercept(self, name: ServiceName, metadata: object) -> Self:
        return self._derive(intercepts={_service_name(name): metadata})

    def metadata(self, name: ServiceName) -> object:
        name = _service_name(name)
        layers: list[object] = []
        ctx: Context | None = self
        while ctx is not None:
            if name in ctx._intercepts_local:
                layers.append(ctx._intercepts_local[name])
            ctx = ctx._parent
        if not layers:
            return None
        layers.reverse()
        if all(isinstance(layer, Mapping) for layer in layers):
            merged: dict[str, object] = {}
            for layer in layers:
                assert isinstance(layer, Mapping)
                merged.update(layer)
            return merged
        return layers[-1]

    @overload
    def use(
        self: ContextT,
        value: Component[ConfigT, ContextT],
        config: ConfigT,
    ) -> Fiber[ConfigT, ContextT]: ...

    @overload
    def use(
        self: ContextT,
        value: Component[None, ContextT],
        config: None = None,
    ) -> Fiber[None, ContextT]: ...

    @overload
    def use(
        self: ContextT,
        value: PluginCallback[ContextT, ConfigT] | Plugin[ContextT, ConfigT],
        config: ConfigT,
    ) -> Fiber[ConfigT, ContextT]: ...

    @overload
    def use(
        self: ContextT,
        value: PluginCallback[ContextT, None] | Plugin[ContextT, None],
        config: None = None,
    ) -> Fiber[None, ContextT]: ...

    def use(
        self,
        value: Component[Any, Any] | PluginCallback[Any, Any] | Plugin[Any, Any],
        config: object = None,
    ) -> Fiber[Any, Any]:
        self._fiber._assert_effects_allowed()
        spec = _as_component(value)
        fiber = Fiber(self._runtime, self, spec, config)
        self._runtime.add(fiber)

        async def remove() -> None:
            await fiber.dispose()

        effect = _Effect.completed(self._fiber, [remove], f"component:{fiber.name}")
        effect.before_dispose = lambda: self._runtime.assert_can_wait_for(fiber)
        fiber._parent_effect = effect
        self._fiber._attach_effect(effect)
        fiber.refresh()
        return fiber

    plugin = use

    @overload
    def inject(
        self: ContextT,
        dependencies: Injection,
        callback: PluginCallback[ContextT, ConfigT],
        config: ConfigT,
    ) -> Fiber[ConfigT, ContextT]: ...

    @overload
    def inject(
        self: ContextT,
        dependencies: Injection,
        callback: PluginCallback[ContextT, None],
        config: None = None,
    ) -> Fiber[None, ContextT]: ...

    def inject(
        self,
        dependencies: Injection,
        callback: PluginCallback[Any, Any],
        config: object = None,
    ) -> Fiber[Any, Any]:
        return self.use(
            Component(
                callback,
                _normalize_inject(dependencies),
                name=getattr(callback, "__name__", type(callback).__name__),
            ),
            config,
        )

    async def effect(
        self, callback: EffectCallback, label: str = "anonymous"
    ) -> EffectHandle:
        self._fiber._assert_effects_allowed()
        effect = _Effect(self._fiber, label)
        self._fiber._attach_effect(effect)
        effect.start(callback)
        return await effect.wait()

    def spawn(
        self,
        awaitable: Awaitable[T],
        *,
        policy: TaskPolicy = "drain",
        timeout: float | None = None,
        name: str | None = None,
    ) -> asyncio.Task[T]:
        """Run work owned by this context's current effect scope.

        On unload, ``drain`` waits for completion; ``cancel`` requests immediate
        cancellation. A drain timeout requests cancellation after that grace
        period. Both wait for cooperative cancellation before releasing effects.
        Failures are reported through internal/error with the owning fiber.
        """
        return self._spawn(awaitable, policy=policy, timeout=timeout, name=name)

    def _spawn(
        self,
        awaitable: Awaitable[T],
        *,
        policy: TaskPolicy,
        timeout: float | None,
        name: str | None,
        source: str | None = None,
    ) -> asyncio.Task[T]:
        try:
            _validate_task_options(policy, timeout)
            self._fiber._assert_effects_allowed()
            if not inspect.isawaitable(awaitable):
                raise TypeError("spawn requires an awaitable")
            loop = asyncio.get_running_loop()
            sink = self._fiber._sink()
        except BaseException:
            _close_coroutine(awaitable)
            raise

        started = False

        async def run() -> T:
            nonlocal started
            started = True
            return await awaitable

        context = contextvars.copy_context()
        context.run(_collector.set, sink)
        runner = run()
        try:
            task = loop.create_task(
                runner, name=name or f"cordis:task:{self._fiber.name}", context=context
            )
        except BaseException:
            runner.close()
            _close_coroutine(awaitable)
            raise
        sink.tasks[task] = _TaskOptions(policy, timeout)
        self._fiber._tasks.add(task)

        def completed(task: asyncio.Task[T]) -> None:
            sink.tasks.pop(task, None)
            self._fiber._tasks.discard(task)
            if not started:
                _close_coroutine(awaitable)
                if isinstance(awaitable, asyncio.Future):
                    awaitable.cancel()
            if not task.cancelled() and isinstance(
                error := task.exception(), Exception
            ):
                if source == "internal/error":
                    self._runtime.errors.append((error, self._fiber))
                else:
                    self._runtime.report(error, self._fiber)

        task.add_done_callback(completed)
        return task

    @overload
    def provide(
        self,
        name: ServiceKey[T],
        value: T,
        *,
        check: Callable[[Context], bool] | None = None,
        adapt: Callable[[T, object, Context], T] | None = None,
    ) -> EffectHandle: ...

    @overload
    def provide(
        self,
        name: str,
        value: T,
        *,
        check: Callable[[Context], bool] | None = None,
        adapt: Callable[[T, object, Context], object] | None = None,
    ) -> EffectHandle: ...

    def provide(
        self,
        name: str | ServiceKey[T],
        value: T,
        *,
        check: Callable[["Context"], bool] | None = None,
        adapt: Callable[[T, object, "Context"], object] | None = None,
    ) -> EffectHandle:
        self._fiber._assert_effects_allowed()
        name = _service_name(name)
        realm = self._realm(name)
        existing = self._runtime.services.get(realm)
        if existing:
            raise DuplicateServiceError(
                f"service {name!r} is already provided by component {existing.fiber.name!r}"
            )

        def adapt_value(value: object, metadata: object, ctx: Context) -> object:
            # The heterogeneous store erases T. Restore it only at registration's
            # adapter boundary; ServiceKey[T] checks typed writes and updates.
            assert adapt is not None
            return adapt(cast(T, value), metadata, ctx)

        service = _Service(
            name,
            value,
            self._fiber,
            realm,
            check,
            adapt_value if adapt is not None else None,
        )
        self._runtime.services[realm] = service
        if self._fiber.state is FiberState.ACTIVE:
            self._runtime.notify(self, [name])

        async def remove() -> None:
            if self._runtime.services.get(service.realm) is service:
                del self._runtime.services[service.realm]
                affected = self._runtime.notify(self, [name])
                await asyncio.gather(
                    *(fiber.wait(raise_error=False) for fiber in affected)
                )

        effect = _Effect.completed(self._fiber, [remove], f"provide:{name}")

        def check_dependents() -> None:
            for dependent in self._runtime.fibers:
                if any(value is service for value in dependent.committed.values()):
                    self._runtime.assert_can_wait_for(dependent)

        effect.before_dispose = check_dependents
        return self._fiber._attach_effect(effect)

    @overload
    def set(self, name: ServiceKey[T], value: T) -> None: ...

    @overload
    def set(self, name: str, value: object) -> None: ...

    def set(self, name: ServiceName, value: object) -> None:
        name = _service_name(name)
        service = self._runtime.services.get(self._realm(name))
        if service is None or service.fiber is not self._fiber:
            raise DuplicateServiceError(
                f"component {self._fiber.name!r} does not own service {name!r}"
            )
        service.value = value

    def notify(self, *names: ServiceName) -> None:
        """Re-evaluate dependents after an availability predicate changes."""
        self._runtime.notify(self, map(_service_name, names))

    @overload
    def get(
        self, name: ServiceKey[T], *, include_inactive: bool = False
    ) -> T | None: ...

    @overload
    def get(
        self, name: ServiceKey[T], default: D, *, include_inactive: bool = False
    ) -> T | D: ...

    @overload
    def get(
        self, name: str, default: object = None, *, include_inactive: bool = False
    ) -> Any: ...

    def get(
        self,
        name: ServiceName,
        default: object = None,
        *,
        include_inactive: bool = False,
    ) -> object:
        name = _service_name(name)
        service = self._runtime.resolve(self, name, active=not include_inactive)
        return default if service is None else self._adapt(service)

    def _adapt(self, service: _Service) -> object:
        metadata = self.metadata(service.name)
        if service.adapt:
            return service.adapt(service.value, metadata, self)
        hook = getattr(service.value, "__cordis_intercept__", None)
        return hook(metadata, self) if hook else service.value

    @overload
    def service(self, name: ServiceKey[T]) -> T: ...

    @overload
    def service(self, name: str) -> Any: ...

    def service(self, name: ServiceName) -> object:
        name = _service_name(name)
        # Resolve within the reader's activation view. New derived contexts may
        # still explicitly override a realm; ancestor reassignment cannot alter
        # resources visible to in-flight work and cleanup from the old activation.
        realm = self._fiber._committed_realm(self, name)
        fiber: Fiber[Any, Any] | None = self._fiber
        while fiber is not None:
            service = fiber.committed.get(name)
            if service is not None and fiber.committed_realms.get(name) is realm:
                return self._adapt(service)
            if name in fiber.inject:
                raise InactiveServiceError(
                    f"required service {name!r} is inactive in component {fiber.name!r}"
                )
            if fiber is self._runtime.root._fiber:
                break
            if self._fiber._committed_realm(fiber.parent, name) is not realm:
                break
            fiber = fiber.parent._fiber
        if self._fiber is self._runtime.root._fiber:
            service = self._runtime.resolve(self, name)
            if service:
                return self._adapt(service)
        raise UndeclaredServiceError(
            f"component {self._fiber.name!r} accessed service {name!r} without declaring it"
        )

    @overload
    def __getitem__(self, name: ServiceKey[T]) -> T: ...

    @overload
    def __getitem__(self, name: str) -> Any: ...

    def __getitem__(self, name: ServiceName) -> Any:
        return self.service(name)

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        return self.service(name)

    @overload
    def on(
        self,
        name: EventKey[T],
        callback: Callable[[T], None | Awaitable[None]],
        *,
        prepend: bool = False,
        global_: bool = False,
        task_policy: TaskPolicy = "drain",
        timeout: float | None = None,
    ) -> EffectHandle: ...

    @overload
    def on(
        self,
        name: str,
        callback: Callable[..., object],
        *,
        prepend: bool = False,
        global_: bool = False,
        task_policy: TaskPolicy = "drain",
        timeout: float | None = None,
    ) -> EffectHandle: ...

    def on(
        self,
        name: _EventName,
        callback: Callable[..., object],
        *,
        prepend: bool = False,
        global_: bool = False,
        task_policy: TaskPolicy = "drain",
        timeout: float | None = None,
    ) -> EffectHandle:
        return self._on(
            name,
            callback,
            prepend=prepend,
            global_=global_,
            task_policy=task_policy,
            timeout=timeout,
        )

    def _on(
        self,
        name: _EventName,
        callback: Callable[..., object],
        *,
        prepend: bool = False,
        global_: bool = False,
        task_policy: TaskPolicy = "drain",
        timeout: float | None = None,
    ) -> EffectHandle:
        self._fiber._assert_effects_allowed()
        _validate_task_options(task_policy, timeout)
        listener = _Listener(
            self, callback, self._fiber._sink(), global_, task_policy, timeout
        )
        listeners = self._runtime.listeners[name]
        if prepend:
            listeners.insert(0, listener)
        else:
            listeners.append(listener)

        def remove() -> None:
            if listener in listeners:
                listeners.remove(listener)

        effect = _Effect.completed(self._fiber, [remove], f"event:{name}")
        return self._fiber._attach_effect(effect)

    @overload
    def once(
        self,
        name: EventKey[T],
        callback: Callable[[T], None | Awaitable[None]],
        *,
        prepend: bool = False,
        global_: bool = False,
        task_policy: TaskPolicy = "drain",
        timeout: float | None = None,
    ) -> EffectHandle: ...

    @overload
    def once(
        self,
        name: str,
        callback: Callable[..., object],
        *,
        prepend: bool = False,
        global_: bool = False,
        task_policy: TaskPolicy = "drain",
        timeout: float | None = None,
    ) -> EffectHandle: ...

    def once(
        self,
        name: _EventName,
        callback: Callable[..., object],
        *,
        prepend: bool = False,
        global_: bool = False,
        task_policy: TaskPolicy = "drain",
        timeout: float | None = None,
    ) -> EffectHandle:
        handle: EffectHandle
        called = False

        async def wrapped(*args: object) -> object:
            nonlocal called
            if called:
                return None
            called = True
            await handle()
            return await _await(callback(*args))

        handle = self._on(
            name,
            wrapped,
            prepend=prepend,
            global_=global_,
            task_policy=task_policy,
            timeout=timeout,
        )
        return handle

    @overload
    def emit(
        self, name: EventKey[T], payload: T, /, *, scope: EventScope = None
    ) -> None: ...

    @overload
    def emit(self, name: str, *args: object, scope: EventScope = None) -> None: ...

    def emit(self, name: _EventName, *args: object, scope: EventScope = None) -> None:
        self._runtime.emit(name, *args, scope=scope)

    @overload
    async def parallel(
        self, name: EventKey[T], payload: T, /, *, scope: EventScope = None
    ) -> None: ...

    @overload
    async def parallel(
        self, name: str, *args: object, scope: EventScope = None
    ) -> None: ...

    async def parallel(
        self, name: _EventName, *args: object, scope: EventScope = None
    ) -> None:
        async def invoke(listener: _Listener) -> object:
            return await _await(self._runtime._invoke(listener, name, args))

        results = await asyncio.gather(
            *(invoke(listener) for listener in self._runtime._listeners(name, scope)),
            return_exceptions=True,
        )
        errors = [result for result in results if isinstance(result, Exception)]
        if errors:
            raise ExceptionGroup(f"event {name!r} failed", errors)

    def handle(
        self,
        key: QueryKey[RequestT, ResultT],
        callback: Callable[[RequestT], ResultT | None | Awaitable[ResultT | None]],
        *,
        task_policy: TaskPolicy = "drain",
        timeout: float | None = None,
    ) -> EffectHandle:
        """Register the sole handler for this query key in the runtime."""
        self._fiber._assert_effects_allowed()
        _validate_task_options(task_policy, timeout)
        if key in self._runtime.queries:
            raise DuplicateQueryError(f"query {key.name!r} already has a handler")
        listener = _Listener(
            self,
            callback,
            self._fiber._sink(),
            task_policy=task_policy,
            timeout=timeout,
        )
        self._runtime.queries[key] = listener

        def remove() -> None:
            if self._runtime.queries.get(key) is listener:
                del self._runtime.queries[key]

        effect = _Effect.completed(self._fiber, [remove], f"query:{key.name}")
        return self._fiber._attach_effect(effect)

    async def query(
        self, key: QueryKey[RequestT, ResultT], request: RequestT
    ) -> ResultT | None:
        """Return None for no active handler/no result; False is a valid result."""
        listener = self._runtime.queries.get(key)
        if listener is None:
            return None
        result = await _await(self._runtime._invoke(listener, key, (request,)))
        # handle() and query() share this invariant, identity-based key. Only
        # this registry boundary restores the result erased by _Listener.
        return cast(ResultT | None, result)

    async def serial(
        self, name: str, *args: object, scope: EventScope = None
    ) -> object:
        for listener in self._runtime._listeners(name, scope):
            result = await _await(self._runtime._invoke(listener, name, args))
            if result is not None and result is not False:
                return result
        return None

    def bail(self, name: str, *args: object, scope: EventScope = None) -> object:
        for listener in self._runtime._listeners(name, scope):
            result = self._runtime._invoke(listener, name, args, sync=True)
            if result is not None and result is not False:
                return result
        return None

    def waterfall(
        self,
        name: str,
        *args: object,
        final: Callable[..., object],
        scope: EventScope = None,
    ) -> object:
        listeners = self._runtime._listeners(name, scope)

        def dispatch(index: int, values: tuple[object, ...]) -> object:
            if index == len(listeners):
                return final(*values)
            return self._runtime._invoke(
                listeners[index],
                name,
                (
                    *values,
                    lambda *next_values: dispatch(index + 1, next_values or values),
                ),
            )

        return dispatch(0, args)

    async def flush(self) -> None:
        await self._runtime.flush()

    async def close(self) -> None:
        self._runtime.assert_can_wait_for(None)
        root = self._runtime.root
        task = root._close_task
        if task is None:
            task = asyncio.create_task(root._close())
            object.__setattr__(root, "_close_task", task)
        await asyncio.shield(task)

    async def _close(self) -> None:
        object.__setattr__(self, "_closed", True)
        root = self._runtime.root._fiber
        root._set_state(FiberState.UNLOADING)
        for fiber in self._runtime.fibers.copy():
            fiber.refresh()
        await self._runtime.flush()
        try:
            await _dispose_actions(root._scope, "root context")
        finally:
            root._set_state(FiberState.DISPOSED)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.close()


def _as_component(
    value: Component[Any, Any] | PluginCallback[Any, Any] | Plugin[Any, Any],
) -> Component[Any, Any]:
    if isinstance(value, Component):
        return value
    callback = value if callable(value) else getattr(value, "apply", None)
    if not callable(callback):
        raise TypeError("a plugin must be callable or expose an apply() method")
    return Component(
        callback,
        _normalize_inject(getattr(value, "inject", ())),
        frozenset(getattr(value, "provide", ())),
        getattr(value, "name", None) or getattr(callback, "__name__", None),
        getattr(value, "validate", None),
        getattr(value, "update", None),
    )
