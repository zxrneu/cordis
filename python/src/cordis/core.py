from __future__ import annotations

import asyncio
import contextvars
import inspect
import math
from collections import ChainMap, defaultdict, deque
from collections.abc import (
    AsyncIterable,
    AsyncIterator,
    Awaitable,
    Callable,
    Generator,
    Iterable,
    Iterator,
    Mapping,
    MutableMapping,
    MutableSequence,
)
from contextlib import (
    AbstractAsyncContextManager,
    AbstractContextManager,
    AsyncExitStack,
    ExitStack,
    asynccontextmanager,
)
from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType, TracebackType
from typing import (
    TYPE_CHECKING,
    Any,
    Generic,
    Literal,
    Protocol,
    Self,
    TypeAlias,
    TypeVar,
    TypeVarTuple,
    Unpack,
    cast,
    overload,
)

from .accessors import Accessor
from .keys import EventKey, QueryKey, RequestT, ResultT, ServiceKey
from .registry import ComponentRegistry

if TYPE_CHECKING:
    from .logger import ContextLogger
    from .schema import SchemaLike
    from .timer import TimerService
    from .services import Service


Cleanup: TypeAlias = Callable[[], object]
ConfigT = TypeVar("ConfigT")
ContextT = TypeVar("ContextT", bound="Context")
ContextCov = TypeVar("ContextCov", bound="Context", covariant=True)
ContextContra = TypeVar("ContextContra", bound="Context", contravariant=True)
ConfigContra = TypeVar("ConfigContra", contravariant=True)
EventArgsT = TypeVarTuple("EventArgsT")
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


PluginResult: TypeAlias = (
    "EffectResult | SupportsStart | SupportsStop | SupportsClose | Service[Any]"
)
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


class _LayerMap(ChainMap[str, T]):
    """A mutable local layer with read-through access to its ancestors."""

    @property
    def local(self) -> MutableMapping[str, T]:
        return self.maps[0]

    def derive(self, values: Mapping[str, T] | None = None) -> Self:
        return type(self)(dict(values or {}), *self.maps)

    def layers_for(self, name: str) -> tuple[T, ...]:
        return tuple(layer[name] for layer in self.maps if name in layer)


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


class DuplicateAccessorError(CordisError):
    pass


class MissingProvisionError(CordisError):
    pass


class TaskLimitError(CordisError):
    """The runtime has no capacity for another managed task."""


class FiberState(str, Enum):
    PENDING = "pending"
    LOADING = "loading"
    ACTIVE = "active"
    FAILED = "failed"
    UNLOADING = "unloading"
    DISPOSED = "disposed"


@dataclass(frozen=True, slots=True)
class ReadinessIssue:
    uid: int | None
    name: str
    state: FiberState
    dependencies: tuple[str, ...]
    parent: str | None
    error: Exception | None


class ReadinessError(CordisError):
    def __init__(
        self,
        issues: tuple[ReadinessIssue, ...],
        cycles: tuple[tuple[str, ...], ...] = (),
    ) -> None:
        self.issues = issues
        self.cycles = cycles
        details = []
        for issue in issues:
            reason = ", ".join(issue.dependencies)
            if issue.parent is not None:
                reason += f"; parent: {issue.parent}"
            if issue.error is not None:
                reason += f"; {issue.error}"
            details.append(
                f"{issue.name}#{issue.uid} ({issue.state.value}"
                f"{': ' + reason if reason else ''})"
            )
        details.extend("declared cycle: " + " -> ".join(cycle) for cycle in cycles)
        super().__init__("runtime is not ready: " + "; ".join(details))


class ReadinessTimeoutError(ReadinessError, TimeoutError):
    """Lifecycle work did not settle within the readiness deadline."""


@dataclass(frozen=True, slots=True)
class ShutdownBlocker:
    uid: int | None
    name: str
    state: FiberState
    tasks: tuple[str, ...]


class ShutdownTimeoutError(CordisError, TimeoutError):
    """Shutdown continues in the background; resources remain owned."""

    def __init__(self, blockers: tuple[ShutdownBlocker, ...]) -> None:
        self.blockers = blockers
        details = "; ".join(
            f"{item.name}#{item.uid} ({item.state.value}"
            f"{': ' + ', '.join(item.tasks) if item.tasks else ''})"
            for item in blockers
        )
        super().__init__(f"shutdown deadline exceeded; cleanup continues: {details}")


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

    @property
    def effects(self) -> tuple[EffectInfo, ...]: ...

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

    async def decode_schema(
        self, raw: object, schema: SchemaLike[ConfigT]
    ) -> BoundComponent[ConfigT, ContextContra]:
        """Decode external data with a supported schema adapter."""
        from .schema import decode_schema

        return self.configure(await decode_schema(schema, raw))


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
    context: Context | None = None


@dataclass(frozen=True, slots=True)
class EffectInfo:
    """An immutable snapshot of a registered effect and its children."""

    label: str
    children: tuple[EffectInfo, ...] = ()


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
    _validate_timeout(timeout)


def _validate_timeout(timeout: float | None) -> None:
    if timeout is not None and (not math.isfinite(timeout) or timeout < 0):
        raise ValueError("timeout must be a finite, non-negative number")


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


@dataclass(frozen=True, slots=True)
class _UpdateCall:
    fiber: Fiber[Any, Any]
    finished: asyncio.Future[None]


_update_calls: contextvars.ContextVar[tuple[_UpdateCall, ...]] = contextvars.ContextVar(
    "cordis_update_calls", default=()
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
        from .services import Service

        if not callable(value) or isinstance(value, Service):
            start = getattr(value, "start", None)
            stop = getattr(value, "stop", None) or getattr(value, "close", None)
            if callable(start) or callable(stop):
                if callable(stop):
                    sink.actions.append(stop)
                if callable(start):
                    await _execute(start, guard, sink)
                return
            if isinstance(value, Service):
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
        await asyncio.shield(self._start_disposal())

    def _start_disposal(self) -> asyncio.Task[None]:
        self._assert_can_dispose()
        if self._disposal is None:
            self._disposal = asyncio.create_task(self._dispose())
        return self._disposal

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
        self.provided: dict[object, _Service] = {}
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
        self._updates: set[asyncio.Future[None]] = set()
        self._update_lock = asyncio.Lock()
        self._on_dispose: Callable[[], Awaitable[None]] | None = None
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

    @property
    def effects(self) -> tuple[EffectInfo, ...]:
        def inspect_actions(
            actions: Iterable[Cleanup | _Effect],
        ) -> tuple[EffectInfo, ...]:
            return tuple(
                EffectInfo(action.label, inspect_actions(action.actions))
                for action in actions
                if isinstance(action, _Effect) and action.armed
            )

        return inspect_actions(self._actions)

    def _assert_effects_allowed(self, scope: _Sink | None = None) -> None:
        if self.state not in (FiberState.LOADING, FiberState.ACTIVE):
            raise InactiveContextError(
                f"cannot create effect in {self.state.value} component {self.name!r}"
            )
        self._sink(scope)

    def _sink(self, scope: _Sink | None = None) -> _Sink:
        current = _collector.get()
        sink = scope if scope is not None else self._scope
        if current is not None and current.fiber is self:
            if scope is None:
                sink = current
            else:
                ancestor: _Sink | None = current
                while ancestor is not None:
                    if ancestor is scope:
                        sink = current
                        break
                    ancestor = ancestor.parent
        if sink.token is not self._load_token or not sink.open:
            raise InactiveContextError(
                f"stale or closing scope attempted an effect in component {self.name!r}"
            )
        return sink

    def _attach_effect(
        self, effect: _Effect, scope: _Sink | None = None
    ) -> EffectHandle:
        self._assert_effects_allowed(scope)
        effect.attach(self._sink(scope))
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
        # Eager factories must not run lifecycle callbacks before the owning
        # Fiber and its caller have finished registering the new drive.
        started: asyncio.Future[None] = asyncio.get_running_loop().create_future()

        async def drive() -> None:
            await started
            await self._drive()

        self.inertia = asyncio.create_task(
            drive(), name=f"cordis:fiber:{self.name}:{self.uid}"
        )
        started.set_result(None)

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
            self._committed_contexts[context] = dict(context._realms.local)
            context = context._parent

    def _committed_realm(self, ctx: Context, name: str) -> object:
        context: Context | None = ctx
        while context is not None:
            realms = self._committed_contexts.get(context, context._realms.local)
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
                    # An in-place update may still be using this activation's
                    # resources. Wait only for its hook, not the surrounding
                    # transaction, which may itself need lifecycle transitions.
                    if self._updates:
                        await asyncio.gather(
                            *(asyncio.shield(done) for done in tuple(self._updates))
                        )
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

                if self.target is None:
                    # Dependency readiness can change after scheduling but
                    # before the drive starts; this is pending, not a failure.
                    self._set_state(FiberState.UNLOADING)
                    self.runtime.provider_leaving(self)
                    continue

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
        on_dispose, self._on_dispose = self._on_dispose, None
        if on_dispose is not None:
            await on_dispose()

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

    async def update(
        self, config: ConfigT, *, rollback: bool = True, persist: bool = True
    ) -> None:
        self.runtime.assert_can_wait_for(self)
        self._assert_update_allowed()
        await _settle(
            self._dispatch_update(config, rollback=rollback, persist=persist),
            name=f"cordis:update:{self.name}",
        )

    async def _dispatch_update(
        self, config: ConfigT, *, rollback: bool, persist: bool
    ) -> None:
        result = self.ctx._waterfall(
            "internal/update",
            self,
            config,
            not persist,
            final=lambda *_: self._update(config, rollback=rollback),
            scope=lambda ctx: ctx._fiber is self,
            managed=False,
        )
        await _await(result)

    async def _update(self, config: ConfigT, *, rollback: bool) -> None:
        async with self._update_lock:
            await self._apply_update(config, rollback=rollback)

    def _assert_update_allowed(self) -> None:
        if (
            not self.desired
            or self.state is FiberState.DISPOSED
            or self.runtime.root._close_task is not None
        ):
            raise InactiveContextError(
                f"cannot update closing or disposed component {self.name!r}"
            )

    async def _apply_update(self, config: ConfigT, *, rollback: bool) -> None:
        self._assert_update_allowed()
        config = await self._validate_config(config)
        self._assert_update_allowed()
        if config == self.config:
            return
        previous = self.config
        if (
            self.component is not None
            and self.component.update is not None
            and self.state is FiberState.ACTIVE
        ):
            finished: asyncio.Future[None] = asyncio.get_running_loop().create_future()
            self._updates.add(finished)
            token = _update_calls.set(
                (*_update_calls.get(), _UpdateCall(self, finished))
            )
            try:
                await _await(self.component.update(self.ctx, previous, config))
                self.config = config
            finally:
                _update_calls.reset(token)
                self._updates.remove(finished)
                finished.set_result(None)
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
    def __init__(self, *, max_tasks: int | None, error_history_limit: int) -> None:
        self.uid = 0
        self.fibers: list[Fiber[Any, Any]] = []
        self.services: dict[object, _Service] = {}
        self.listeners: dict[_EventName, list[_Listener]] = defaultdict(list)
        self.queries: dict[QueryKey[Any, Any], _Listener] = {}
        self.accessors: dict[str, Accessor] = {}
        self.errors: deque[tuple[Exception, Fiber[Any, Any] | None]] = deque(
            maxlen=error_history_limit
        )
        self.error_count = 0
        self.max_tasks = max_tasks
        self.tasks: set[asyncio.Task[object]] = set()
        self.task_reservations = 0
        self.root: Context
        self.registry = ComponentRegistry[Any](self)

    def next_uid(self) -> int:
        self.uid += 1
        return self.uid

    def assert_can_wait_for(self, target: Fiber[Any, Any] | None) -> None:
        current = asyncio.current_task()
        owners = {
            fiber
            for fiber in [self.root._fiber, *self.fibers]
            if current in fiber._tasks
        }
        owners.update(
            call.fiber
            for call in _update_calls.get()
            if call.fiber.runtime is self and not call.finished.done()
        )
        if not owners:
            return
        pending = list(owners)
        visited: set[Fiber[Any, Any]] = set()
        while pending:
            fiber = pending.pop()
            if fiber in visited:
                continue
            visited.add(fiber)
            if target is None or fiber is target:
                raise InactiveContextError(
                    "owned work cannot wait for teardown of its own scope "
                    "or a dependency; request the change from the host instead"
                )
            pending.extend(service.fiber for service in fiber.committed.values())
            if fiber is not self.root._fiber:
                pending.append(fiber.parent._fiber)

    def add(self, fiber: Fiber[Any, Any]) -> None:
        self.fibers.append(fiber)
        self.emit("internal/plugin", fiber)

    def remove(self, fiber: Fiber[Any, Any]) -> None:
        if fiber in self.fibers:
            self.fibers.remove(fiber)
        fiber.uid = None
        self.emit("internal/plugin", fiber)
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

    def notify(self, ctx: "Context", names: Iterable[str]) -> list[Fiber[Any, Any]]:
        return self._notify_realms((name, ctx._realm(name)) for name in set(names))

    def _notify_realms(
        self, realms: Iterable[tuple[str, object]]
    ) -> list[Fiber[Any, Any]]:
        realms = tuple(realms)
        affected: list[Fiber[Any, Any]] = []
        for fiber in self.fibers.copy():
            if not any(
                fiber.ctx._realm(name) is realm
                for name, realm in realms
                if name in fiber.inject
            ):
                continue
            fiber.refresh()
            affected.append(fiber)
        for name, realm in realms:
            service = self.services.get(realm)

            def same_realm(
                ctx: Context, name: str = name, realm: object = realm
            ) -> bool:
                return ctx._realm(name) is realm

            self.emit(
                "internal/service",
                service.context if service else self.root,
                name,
                service.value if service else None,
                scope=same_realm,
            )
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
        # A fiber may publish through derived contexts, including several
        # realms for the same name. Notify the actual registration realms.
        self._notify_realms(
            (service.name, service.realm)
            for service in self.services.values()
            if service.fiber is fiber
        )
        for child in self.fibers.copy():
            if child.parent._fiber is fiber:
                child.refresh()

    def provider_active(self, fiber: Fiber[Any, Any]) -> None:
        self._notify_realms(
            (service.name, service.realm)
            for service in self.services.values()
            if service.fiber is fiber
        )
        for child in self.fibers.copy():
            if child.parent._fiber is fiber:
                child.refresh()

    def reassign_context_realms(
        self, ctx: "Context", realms: Mapping[str, object]
    ) -> None:
        local_realms = ctx._realms.local
        keys = set(local_realms) | set(realms)
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

        local_realms.clear()
        local_realms.update(realms)
        for source, target, service in moves:
            if self.services.get(source) is service:
                del self.services[source]
            self.services[target] = service
            service.realm = target
        for fiber in self.fibers.copy():
            fiber.refresh()

    def report(self, error: Exception, fiber: Fiber[Any, Any] | None = None) -> None:
        self.record_error(error, fiber)
        if self.listeners.get("internal/error"):
            self.emit("internal/error", error, fiber)

    def record_error(self, error: Exception, fiber: Fiber[Any, Any] | None) -> None:
        self.error_count += 1
        self.errors.append((error, fiber))

    def check_task_capacity(self) -> None:
        if (
            self.max_tasks is not None
            and len(self.tasks) + self.task_reservations >= self.max_tasks
        ):
            # Done callbacks can be queued behind the caller resuming from an
            # await. Such tasks no longer consume running-work capacity.
            self.tasks.difference_update(
                tuple(task for task in self.tasks if task.done())
            )
            if len(self.tasks) + self.task_reservations >= self.max_tasks:
                raise TaskLimitError(f"managed task limit reached ({self.max_tasks})")

    def readiness_issues(self) -> tuple[ReadinessIssue, ...]:
        return tuple(
            ReadinessIssue(
                fiber.uid,
                fiber.name,
                fiber.state,
                tuple(
                    name
                    for name in fiber.inject
                    if self.resolve(fiber.ctx, name) is None
                ),
                (
                    fiber.parent._fiber.name
                    if fiber.parent._fiber.state is not FiberState.ACTIVE
                    else None
                ),
                fiber.error or fiber.cleanup_error,
            )
            for fiber in self.fibers
            if fiber.state is not FiberState.ACTIVE
            and (fiber.desired or fiber.inertia is not None)
        )

    def dependency_cycles(self) -> tuple[tuple[str, ...], ...]:
        # Declared provisions are diagnostic hints. Dynamic provisions cannot be
        # inferred before apply(), and ambiguous providers must not imply a cycle.
        pending = [fiber for fiber in self.fibers if fiber.state is FiberState.PENDING]
        providers: dict[object, list[Fiber[Any, Any]]] = defaultdict(list)
        for fiber in pending:
            if fiber.component is not None:
                for name in fiber.component.provide:
                    providers[fiber.ctx._realm(name)].append(fiber)
        graph = {
            fiber: [
                matches[0]
                for name in fiber.inject
                if self.resolve(fiber.ctx, name) is None
                and len(matches := providers.get(fiber.ctx._realm(name), [])) == 1
            ]
            for fiber in pending
        }
        cycles = []
        visited: set[Fiber[Any, Any]] = set()
        # Iterative traversal also handles dependency chains beyond Python's
        # recursion limit. Only back edges are reported, not every possible path.
        for start in pending:
            if start in visited:
                continue
            path = [start]
            positions = {start: 0}
            visited.add(start)
            stack = [iter(graph[start])]
            while stack:
                neighbor = next(stack[-1], None)
                if neighbor is None:
                    stack.pop()
                    positions.pop(path.pop())
                elif neighbor in positions:
                    cycle = [*path[positions[neighbor] :], neighbor]
                    cycles.append(tuple(f"{fiber.name}#{fiber.uid}" for fiber in cycle))
                elif neighbor not in visited:
                    visited.add(neighbor)
                    positions[neighbor] = len(path)
                    path.append(neighbor)
                    stack.append(iter(graph[neighbor]))
        return tuple(cycles)

    def shutdown_blockers(self) -> tuple[ShutdownBlocker, ...]:
        return tuple(
            ShutdownBlocker(
                fiber.uid,
                fiber.name,
                fiber.state,
                tuple(
                    sorted(
                        [
                            *(task.get_name() for task in fiber.tasks),
                            *(f"cordis:update:{fiber.name}" for _ in fiber._updates),
                        ]
                    )
                ),
            )
            for fiber in [self.root._fiber, *self.fibers]
            if fiber.state is not FiberState.DISPOSED
        )

    def _listeners(
        self,
        name: _EventName,
        scope: EventScope = None,
        *,
        mode: str | None = None,
        args: tuple[object, ...] = (),
    ) -> list[_Listener]:
        if mode and not (isinstance(name, str) and name.startswith("internal/")):
            self.emit("internal/dispatch", mode, name, args, scope)
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
        managed: bool = True,
        preserve_result: bool = False,
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
            if preserve_result and (
                inspect.iscoroutinefunction(listener.callback)
                or inspect.iscoroutinefunction(
                    getattr(listener.callback, "__call__", None)
                )
            ):
                raise TypeError(f"{name} listeners must be synchronous")
            result = listener.callback(*args)
            if preserve_result or not inspect.isawaitable(result):
                return result
            if sync:
                _close_coroutine(result)
                raise TypeError("bail listeners must be synchronous")
            if not managed:

                async def run() -> object:
                    token = _collector.set(listener.sink)
                    try:
                        return await result
                    finally:
                        _collector.reset(token)

                return run()
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

    def emit(
        self,
        name: _EventName,
        *args: object,
        scope: EventScope = None,
        safe: bool = True,
    ) -> None:
        for listener in self._listeners(name, scope, mode="emit", args=args):
            try:
                self._invoke(listener, name, args)
            except Exception as error:
                if not safe:
                    raise
                if name == "internal/error":
                    self.record_error(error, listener.ctx._fiber)
                else:
                    self.report(error, listener.ctx._fiber)

    async def flush(self) -> None:
        while True:
            pending = [fiber for fiber in self.fibers if fiber.inertia is not None]
            for fiber in pending:
                self.assert_can_wait_for(fiber)
            tasks = [fiber.inertia for fiber in pending if fiber.inertia is not None]
            if not tasks:
                return
            await asyncio.gather(*(asyncio.shield(task) for task in tasks))


class Context:
    """Manage plugin dependencies, resources, and background work."""

    _runtime: _Runtime
    _parent: Context | None
    _fiber: Fiber[Any, Self]
    _realms: _LayerMap[object]
    _intercepts: _LayerMap[object]
    _closed: bool
    _close_task: asyncio.Task[None] | None
    _resource_scope: _Sink | None

    def __init__(
        self, *, max_tasks: int | None = 1024, error_history_limit: int = 256
    ) -> None:
        if max_tasks is not None and (type(max_tasks) is not int or max_tasks <= 0):
            raise ValueError("max_tasks must be a positive integer or None")
        if type(error_history_limit) is not int or error_history_limit < 0:
            raise ValueError("error_history_limit must be a non-negative integer")
        runtime = _Runtime(max_tasks=max_tasks, error_history_limit=error_history_limit)
        self._runtime = runtime
        self._parent = None
        self._realms = _LayerMap()
        self._intercepts = _LayerMap()
        self._closed = False
        self._close_task = None
        self._resource_scope = None
        root = Fiber[Any, Self](runtime, cast(Self, self), None, None, root=True)
        self._fiber = root
        runtime.root = self

    def _derive(
        self,
        *,
        fiber: Fiber[Any, Self] | None = None,
        realms: Mapping[str, object] | None = None,
        intercepts: Mapping[str, object] | None = None,
    ) -> Self:
        ctx = object.__new__(type(self))
        ctx._runtime = self._runtime
        ctx._parent = self
        ctx._realms = self._realms.derive(realms)
        ctx._intercepts = self._intercepts.derive(intercepts)
        ctx._closed = False
        ctx._close_task = None
        ctx._fiber = fiber or self._fiber
        ctx._resource_scope = None if fiber is not None else self._resource_scope
        return ctx

    def extend(
        self, metadata: Mapping[str, object] | None = None, **values: object
    ) -> Self:
        """Derive a Context with explicitly declared application metadata."""
        ctx = self._derive()
        for name, value in {**(metadata or {}), **values}.items():
            if name.startswith("_") or any(
                name in cls.__dict__ for cls in type(self).__mro__
            ):
                raise ValueError(f"cannot override Context member {name!r}")
            if any(service.name == name for service in self._runtime.services.values()):
                raise DuplicateServiceError(f"metadata shadows service {name!r}")
            object.__setattr__(ctx, name, value)
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

    @property
    def error_count(self) -> int:
        """Lifetime reported errors, including errors evicted from history."""
        return self._runtime.error_count

    def clear_errors(self) -> None:
        """Release retained errors without resetting the lifetime counter."""
        self._runtime.errors.clear()

    @property
    def task_count(self) -> int:
        """Unfinished managed tasks across the entire runtime."""
        return self._runtime.task_reservations + sum(
            not task.done() for task in self._runtime.tasks
        )

    @property
    def registry(self) -> ComponentRegistry[Self]:
        return cast(ComponentRegistry[Self], self._runtime.registry)

    @property
    def logger(self) -> ContextLogger:
        from .logger import LoggerService

        return LoggerService().bind(self)

    @property
    def timer(self) -> TimerService:
        from .timer import TimerService

        return TimerService(self)

    def _realm(self, name: str) -> object:
        try:
            return self._realms[name]
        except KeyError:
            realm = object()
            self._runtime.root._realms.local[name] = realm
            return realm

    def isolate(self, *names: ServiceName, realm: object | None = None) -> Self:
        if realm is not None and len(names) != 1:
            raise ValueError("an explicit realm can isolate exactly one service")
        return self._derive(
            realms={
                _service_name(name): object() if realm is None else realm
                for name in names
            }
        )

    def intercept(self, name: ServiceName, metadata: object) -> Self:
        return self._derive(intercepts={_service_name(name): metadata})

    def metadata(self, name: ServiceName) -> object:
        name = _service_name(name)
        layers = self._intercepts.layers_for(name)
        if not layers:
            return None
        if all(isinstance(layer, Mapping) for layer in layers):
            merged: dict[Any, Any] = {}
            for layer in reversed(layers):
                merged.update(cast(Mapping[Any, Any], layer))
            return merged
        return layers[0]

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
        self._fiber._assert_effects_allowed(self._resource_scope)
        spec = _as_component(value)
        fiber = Fiber(self._runtime, self, spec, config)
        self._runtime.add(fiber)

        async def remove() -> None:
            await fiber.dispose()

        effect = _Effect.completed(self._fiber, [remove], f"component:{fiber.name}")
        effect.before_dispose = lambda: self._runtime.assert_can_wait_for(fiber)
        fiber._parent_effect = effect
        self._fiber._attach_effect(effect, self._resource_scope)
        fiber.refresh()
        return fiber

    plugin = use

    @overload
    def include(
        self: ContextT,
        value: Component[ConfigT, ContextT],
        config: ConfigT,
        *,
        isolate: Iterable[ServiceName] = (),
        intercept: Mapping[ServiceName, object] | None = None,
    ) -> Fiber[ConfigT, ContextT]: ...

    @overload
    def include(
        self: ContextT,
        value: Component[None, ContextT],
        config: None = None,
        *,
        isolate: Iterable[ServiceName] = (),
        intercept: Mapping[ServiceName, object] | None = None,
    ) -> Fiber[None, ContextT]: ...

    @overload
    def include(
        self: ContextT,
        value: PluginCallback[ContextT, ConfigT] | Plugin[ContextT, ConfigT],
        config: ConfigT,
        *,
        isolate: Iterable[ServiceName] = (),
        intercept: Mapping[ServiceName, object] | None = None,
    ) -> Fiber[ConfigT, ContextT]: ...

    @overload
    def include(
        self: ContextT,
        value: PluginCallback[ContextT, None] | Plugin[ContextT, None],
        config: None = None,
        *,
        isolate: Iterable[ServiceName] = (),
        intercept: Mapping[ServiceName, object] | None = None,
    ) -> Fiber[None, ContextT]: ...

    def include(
        self,
        value: Component[Any, Any] | PluginCallback[Any, Any] | Plugin[Any, Any],
        config: object = None,
        *,
        isolate: Iterable[ServiceName] = (),
        intercept: Mapping[ServiceName, object] | None = None,
    ) -> Fiber[Any, Any]:
        """Mount a component through a derived context in one expression."""
        ctx: Context = self.isolate(*isolate)
        for name, metadata in (intercept or {}).items():
            ctx = ctx.intercept(name, metadata)
        return ctx.use(value, config)

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

    def enter_context(self, manager: AbstractContextManager[T]) -> T:
        """Acquire a resource and release it when this scope closes.

        Return the manager's entry value. Exit receives no exception details;
        use a regular with statement when lexical exception handling is needed.
        """
        self._fiber._assert_effects_allowed(self._resource_scope)
        stack = ExitStack()
        try:
            value = stack.enter_context(manager)
            self.defer(stack.close, label="resource")
        except BaseException:
            stack.close()
            raise
        return value

    async def enter_async_context(self, manager: AbstractAsyncContextManager[T]) -> T:
        """Acquire an async resource and release it when this scope closes.

        Acquisition must finish in the same live scope in which it started.
        Exit receives no exception details, as with enter_context().
        """
        self._fiber._assert_effects_allowed(self._resource_scope)
        sink = self._fiber._sink(self._resource_scope)
        stack = AsyncExitStack()
        try:
            value = await stack.enter_async_context(manager)
            if self._fiber._sink(self._resource_scope) is not sink:
                raise InactiveContextError("resource acquisition outlived its scope")
            self.defer(stack.aclose, label="resource")
        except BaseException:
            await _settle(stack.aclose(), name="cordis:resource-rollback")
            raise
        return value

    @asynccontextmanager
    async def scope(self) -> AsyncIterator[Self]:
        """Own resources and work registered through a scoped context.

        The yielded context shares services and dependencies with its parent.
        Exiting waits for owned work, then releases resources. Registrations
        through the original context retain their original owner.
        """
        effect = _Effect(self._fiber, "scope")
        self._fiber._attach_effect(effect, self._resource_scope)
        ctx = self._derive()
        ctx._resource_scope = effect.sink
        try:
            yield ctx
        finally:
            effect._assert_can_dispose()
            await _settle(effect.dispose(), name="cordis:scope-close")

    async def effect(
        self, callback: EffectCallback, label: str = "anonymous"
    ) -> EffectHandle:
        """Run a legacy initializer; prefer explicit resource registration or scope()."""
        self._fiber._assert_effects_allowed(self._resource_scope)
        effect = _Effect(self._fiber, label)
        self._fiber._attach_effect(effect, self._resource_scope)
        effect.start(callback)
        return await effect.wait()

    def defer(self, cleanup: Cleanup, *, label: str = "defer") -> EffectHandle:
        """Register an already-created resource cleanup in the current scope."""
        self._fiber._assert_effects_allowed(self._resource_scope)
        if not callable(cleanup):
            raise TypeError("cleanup must be callable")
        effect = _Effect.completed(self._fiber, [cleanup], label)
        return self._fiber._attach_effect(effect, self._resource_scope)

    def spawn(
        self,
        awaitable: Awaitable[T],
        *,
        policy: TaskPolicy = "drain",
        timeout: float | None = None,
        name: str | None = None,
    ) -> asyncio.Task[T]:
        """Run work owned by this context's resource scope.

        On unload, ``drain`` waits for completion; ``cancel`` requests immediate
        cancellation. A drain timeout requests cancellation after that grace
        period. Both wait for cooperative cancellation before releasing resources.
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
            self._fiber._assert_effects_allowed(self._resource_scope)
            if not inspect.isawaitable(awaitable):
                raise TypeError("spawn requires an awaitable")
            loop = asyncio.get_running_loop()
            sink = self._fiber._sink(self._resource_scope)
            self._runtime.check_task_capacity()
        except BaseException:
            _close_coroutine(awaitable)
            raise

        started = False
        reserved = True
        options = _TaskOptions(policy, timeout)

        def register(task: asyncio.Task[object]) -> None:
            nonlocal reserved
            if reserved:
                self._runtime.task_reservations -= 1
                reserved = False
            sink.tasks[task] = options
            self._fiber._tasks.add(task)
            self._runtime.tasks.add(task)

        async def run() -> T:
            nonlocal started
            started = True
            task = asyncio.current_task()
            assert task is not None
            # Eager work needs ownership before executing user code, including
            # self-wait guards. Replace the reservation rather than count both.
            register(task)
            return await awaitable

        context = contextvars.copy_context()
        context.run(_collector.set, sink)
        runner = run()
        # Eager task factories may execute run() before create_task returns.
        # Reserve admission before user code can recursively spawn more work.
        self._runtime.task_reservations += 1
        try:
            task = loop.create_task(
                runner, name=name or f"cordis:task:{self._fiber.name}", context=context
            )
            register(task)
        except BaseException:
            runner.close()
            _close_coroutine(awaitable)
            raise
        finally:
            if reserved:
                self._runtime.task_reservations -= 1

        def completed(task: asyncio.Task[T]) -> None:
            sink.tasks.pop(task, None)
            self._fiber._tasks.discard(task)
            self._runtime.tasks.discard(task)
            if not started:
                _close_coroutine(awaitable)
                if isinstance(awaitable, asyncio.Future):
                    awaitable.cancel()
            if not task.cancelled() and isinstance(
                error := task.exception(), Exception
            ):
                if source == "internal/error":
                    self._runtime.record_error(error, self._fiber)
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
        self._fiber._assert_effects_allowed(self._resource_scope)
        name = _service_name(name)
        if name in self._runtime.accessors:
            raise DuplicateAccessorError(
                f"property {name!r} is already registered as an accessor"
            )
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
        service.context = self
        self._fiber.provided[realm] = service
        if self._fiber.state is FiberState.ACTIVE:
            self._runtime.notify(self, [name])

        async def remove() -> None:
            if self._runtime.services.get(service.realm) is service:
                del self._runtime.services[service.realm]
                affected = self._runtime.notify(self, [name])
                await asyncio.gather(
                    *(fiber.wait(raise_error=False) for fiber in affected)
                )
            self._fiber.provided.pop(realm, None)

        effect = _Effect.completed(self._fiber, [remove], f"provide:{name}")

        def check_dependents() -> None:
            for dependent in self._runtime.fibers:
                if any(value is service for value in dependent.committed.values()):
                    self._runtime.assert_can_wait_for(dependent)

        effect.before_dispose = check_dependents
        return self._fiber._attach_effect(effect, self._resource_scope)

    @overload
    def set(self, name: ServiceKey[T], value: T) -> None: ...

    @overload
    def set(self, name: str, value: object) -> None: ...

    def set(self, name: ServiceName, value: object) -> None:
        name = _service_name(name)

        def assign(*_: object) -> None:
            service = self._runtime.services.get(self._realm(name))
            if service is None or service.fiber is not self._fiber:
                raise DuplicateServiceError(
                    f"component {self._fiber.name!r} does not own service {name!r}"
                )
            service.value = value

        self._waterfall(
            "internal/set", self, name, value, final=assign, managed=False, sync=True
        )

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
        return self._waterfall(
            "internal/get",
            self,
            name,
            final=lambda *_: self._service(name),
            managed=False,
            sync=True,
        )

    def _service(self, name: str) -> object:
        from .services import definition_context

        definition = definition_context(self)
        # Resolve within the reader's activation view. New derived contexts may
        # still explicitly override a realm; ancestor reassignment cannot alter
        # resources visible to in-flight work and cleanup from the old activation.
        realm = definition._fiber._committed_realm(definition, name)
        fiber: Fiber[Any, Any] | None = definition._fiber
        while fiber is not None:
            own = next(
                (
                    s
                    for s in fiber.provided.values()
                    if s.name == name and s.realm is realm
                ),
                None,
            )
            if own is not None:
                return self._adapt(own)
            service = fiber.committed.get(name)
            if service is not None and fiber.committed_realms.get(name) is realm:
                return self._adapt(service)
            if name in fiber.inject:
                raise InactiveServiceError(
                    f"required service {name!r} is inactive in component {fiber.name!r}"
                )
            if fiber is self._runtime.root._fiber:
                break
            if definition._fiber._committed_realm(fiber.parent, name) is not realm:
                break
            fiber = fiber.parent._fiber
        if definition._fiber is self._runtime.root._fiber:
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
        parent = object.__getattribute__(self, "_parent")
        while parent is not None:
            try:
                return object.__getattribute__(parent, name)
            except AttributeError:
                parent = object.__getattribute__(parent, "_parent")
        if name.startswith("_"):
            raise AttributeError(name)
        accessor = self._runtime.accessors.get(name)
        if accessor is not None:
            return accessor.get(self)
        return self.service(name)

    def __setattr__(self, name: str, value: object) -> None:
        if any(
            isinstance(cls.__dict__.get(name), ServiceKey) for cls in type(self).__mro__
        ):
            object.__setattr__(self, name, value)
            return
        if not name.startswith("_"):
            try:
                runtime = object.__getattribute__(self, "_runtime")
            except AttributeError:
                runtime = None
            accessor = runtime.accessors.get(name) if runtime is not None else None
            if accessor is not None:
                if accessor.set is None:
                    raise AttributeError(f"accessor {name!r} is read-only")
                accessor.set(self, value)
                return
            if runtime is not None and "_fiber" in self.__dict__:
                if any(service.name == name for service in runtime.services.values()):
                    self.set(name, value)
                    return
                if self._fiber is not runtime.root._fiber:
                    parent: Context | None = self
                    while parent is not None:
                        if name in parent.__dict__ or any(
                            name in cls.__dict__ for cls in type(self).__mro__
                        ):
                            break
                        parent = parent._parent
                    if parent is None:
                        raise UndeclaredServiceError(
                            f"cannot set property {name!r} without provide or extend"
                        )
        object.__setattr__(self, name, value)

    def accessor(
        self,
        name: str,
        get: Callable[[Context], object],
        *,
        set: Callable[[Context, object], None] | None = None,
        get_for: Callable[[Context, object], object] | None = None,
        set_for: Callable[[Context, object, object], None] | None = None,
    ) -> EffectHandle:
        """Expose a lifecycle-bound computed property on every Context view."""
        self._fiber._assert_effects_allowed(self._resource_scope)
        if not name or name.startswith("_"):
            raise ValueError("accessor name must be a public non-empty name")
        if any(name in cls.__dict__ for cls in type(self).__mro__):
            raise DuplicateAccessorError(
                f"property {name!r} is already defined by Context"
            )
        if name in self._runtime.accessors:
            raise DuplicateAccessorError(f"accessor {name!r} is already registered")
        if any(service.name == name for service in self._runtime.services.values()):
            raise DuplicateAccessorError(
                f"property {name!r} is already registered as a service"
            )
        accessor = Accessor(get, set, get_for, set_for)
        self._runtime.accessors[name] = accessor

        def remove() -> None:
            if self._runtime.accessors.get(name) is accessor:
                del self._runtime.accessors[name]

        effect = _Effect.completed(self._fiber, [remove], f"accessor:{name}")
        return self._fiber._attach_effect(effect, self._resource_scope)

    def mixin(
        self,
        service: object,
        names: Iterable[str] | Mapping[str, str],
        *,
        namespace: str | None = None,
    ) -> tuple[EffectHandle, ...]:
        """Expose selected service attributes as Context accessors."""
        mapping = (
            {name: name for name in names}
            if not isinstance(names, Mapping)
            else dict(names)
        )
        if namespace is not None:
            mapping = {
                f"{namespace}.{exposed}": source for exposed, source in mapping.items()
            }
        for exposed, source in mapping.items():
            if not isinstance(exposed, str) or not isinstance(source, str):
                raise TypeError("mixin names must be strings")
            if (
                any(exposed in cls.__dict__ for cls in type(self).__mro__)
                or (exposed in self._runtime.accessors)
                or any(
                    registered.name == exposed
                    for registered in self._runtime.services.values()
                )
            ):
                raise DuplicateAccessorError(
                    f"property {exposed!r} is already registered"
                )

        handles = []

        def target(ctx: Context) -> object:
            return (
                ctx.service(service)
                if isinstance(service, (str, ServiceKey))
                else service
            )

        for exposed, source in mapping.items():

            def get(ctx: Context, source: str = source) -> object:
                return getattr(target(ctx), source)

            def set_value(ctx: Context, value: object, source: str = source) -> None:
                setattr(target(ctx), source, value)

            def get_for(ctx: Context, receiver: object, source: str = source) -> object:
                value = target(ctx)
                descriptor = inspect.getattr_static(value, source, None)
                if isinstance(descriptor, property) and descriptor.fget is not None:
                    return descriptor.fget(receiver)
                return getattr(value, source)

            def set_for(
                ctx: Context, receiver: object, value: object, source: str = source
            ) -> None:
                descriptor = inspect.getattr_static(target(ctx), source, None)
                if isinstance(descriptor, property) and descriptor.fset is not None:
                    descriptor.fset(receiver, value)
                else:
                    setattr(target(ctx), source, value)

            handles.append(
                self.accessor(
                    exposed, get, set=set_value, get_for=get_for, set_for=set_for
                )
            )
        return tuple(handles)

    @overload
    def on(
        self,
        name: EventKey[Unpack[EventArgsT]],
        callback: Callable[[Unpack[EventArgsT]], None | Awaitable[None]],
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
        name: Any,
        callback: Callable[..., Any],
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
        self._fiber._assert_effects_allowed(self._resource_scope)
        _validate_task_options(task_policy, timeout)
        listener = _Listener(
            self,
            callback,
            self._fiber._sink(self._resource_scope),
            global_,
            task_policy,
            timeout,
        )
        listeners = self._runtime.listeners[name]
        options = {"prepend": prepend, "global": global_}
        for hook in self._runtime._listeners("internal/listener"):
            intercepted = self._runtime._invoke(
                hook, "internal/listener", (self, name, callback, options), sync=True
            )
            if isinstance(intercepted, EffectHandle):
                return intercepted
            if callable(intercepted):
                return self.defer(intercepted, label=f"listener:{name}")
            if intercepted is not None and intercepted is not False:
                raise TypeError("listener interception must return a cleanup handle")
        if prepend:
            listeners.insert(0, listener)
        else:
            listeners.append(listener)

        def remove() -> None:
            if listener in listeners:
                listeners.remove(listener)
            if not listeners:
                self._runtime.listeners.pop(name, None)

        effect = _Effect.completed(self._fiber, [remove], f"event:{name}")
        return self._fiber._attach_effect(effect, self._resource_scope)

    @overload
    def once(
        self,
        name: EventKey[Unpack[EventArgsT]],
        callback: Callable[[Unpack[EventArgsT]], None | Awaitable[None]],
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
        name: Any,
        callback: Callable[..., Any],
        *,
        prepend: bool = False,
        global_: bool = False,
        task_policy: TaskPolicy = "drain",
        timeout: float | None = None,
    ) -> EffectHandle:
        handle: EffectHandle
        called = False

        def wrapped(*args: object) -> object:
            nonlocal called
            if called:
                return None
            called = True
            # Detach synchronously so reentrant dispatch cannot observe it.
            listeners = self._runtime.listeners.get(name, [])
            listeners[:] = [
                listener for listener in listeners if listener.callback is not wrapped
            ]
            if not listeners:
                self._runtime.listeners.pop(name, None)

            # Cleanup is lifecycle work, independent of business task capacity.
            # Leave the effect attached until disposal finishes so teardown waits.
            def completed(task: asyncio.Task[None]) -> None:
                if not task.cancelled() and isinstance(
                    error := task.exception(), Exception
                ):
                    if name == "internal/error":
                        self._runtime.record_error(error, self._fiber)
                    else:
                        self._runtime.report(error, self._fiber)

            handle._effect._start_disposal().add_done_callback(completed)
            return callback(*args)

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
        self,
        name: EventKey[Unpack[EventArgsT]],
        *args: Unpack[EventArgsT],
        scope: EventScope = None,
    ) -> None: ...

    @overload
    def emit(self, name: str, *args: object, scope: EventScope = None) -> None: ...

    def emit(self, name: Any, *args: object, scope: EventScope = None) -> None:
        self._runtime.emit(name, *args, scope=scope, safe=False)

    @overload
    async def parallel(
        self,
        name: EventKey[Unpack[EventArgsT]],
        *args: Unpack[EventArgsT],
        scope: EventScope = None,
    ) -> None: ...

    @overload
    async def parallel(
        self, name: str, *args: object, scope: EventScope = None
    ) -> None: ...

    async def parallel(
        self, name: Any, *args: object, scope: EventScope = None
    ) -> None:
        async def invoke(listener: _Listener) -> object:
            return await _await(self._runtime._invoke(listener, name, args))

        results = await asyncio.gather(
            *(
                invoke(listener)
                for listener in self._runtime._listeners(
                    name, scope, mode="parallel", args=args
                )
            ),
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
        self._fiber._assert_effects_allowed(self._resource_scope)
        _validate_task_options(task_policy, timeout)
        if key in self._runtime.queries:
            raise DuplicateQueryError(f"query {key.name!r} already has a handler")
        listener = _Listener(
            self,
            callback,
            self._fiber._sink(self._resource_scope),
            task_policy=task_policy,
            timeout=timeout,
        )
        self._runtime.queries[key] = listener

        def remove() -> None:
            if self._runtime.queries.get(key) is listener:
                del self._runtime.queries[key]

        effect = _Effect.completed(self._fiber, [remove], f"query:{key.name}")
        return self._fiber._attach_effect(effect, self._resource_scope)

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

    @overload
    async def serial(
        self,
        name: EventKey[Unpack[EventArgsT]],
        *args: Unpack[EventArgsT],
        scope: EventScope = None,
    ) -> object: ...

    @overload
    async def serial(
        self, name: str, *args: object, scope: EventScope = None
    ) -> object: ...

    async def serial(
        self, name: Any, *args: object, scope: EventScope = None
    ) -> object:
        for listener in self._runtime._listeners(name, scope, mode="serial", args=args):
            result = await _await(self._runtime._invoke(listener, name, args))
            if result is not None and result is not False:
                return result
        return None

    @overload
    def bail(
        self,
        name: EventKey[Unpack[EventArgsT]],
        *args: Unpack[EventArgsT],
        scope: EventScope = None,
    ) -> object: ...

    @overload
    def bail(self, name: str, *args: object, scope: EventScope = None) -> object: ...

    def bail(self, name: Any, *args: object, scope: EventScope = None) -> object:
        for listener in self._runtime._listeners(name, scope, mode="bail", args=args):
            result = self._runtime._invoke(listener, name, args, sync=True)
            if result is not None and result is not False:
                return result
        return None

    @overload
    def waterfall(
        self,
        name: EventKey[Unpack[EventArgsT]],
        *args: Unpack[EventArgsT],
        final: Callable[..., object],
        scope: EventScope = None,
    ) -> object: ...

    @overload
    def waterfall(
        self,
        name: str,
        *args: object,
        final: Callable[..., object],
        scope: EventScope = None,
    ) -> object: ...

    def waterfall(
        self,
        name: Any,
        *args: object,
        final: Callable[..., object],
        scope: EventScope = None,
    ) -> object:
        return self._waterfall(name, *args, final=final, scope=scope)

    def _waterfall(
        self,
        name: _EventName,
        *args: object,
        final: Callable[..., object],
        scope: EventScope = None,
        managed: bool = True,
        sync: bool = False,
    ) -> object:
        listeners = self._runtime._listeners(name, scope, mode="waterfall", args=args)

        def dispatch(index: int, values: tuple[object, ...]) -> object:
            if index == len(listeners):
                return final(*values)
            called = False

            def next_step(*next_values: object) -> object:
                nonlocal called
                if called:
                    raise RuntimeError("next() called multiple times")
                called = True
                return dispatch(index + 1, next_values or values)

            return self._runtime._invoke(
                listeners[index],
                name,
                (
                    *values,
                    next_step,
                ),
                managed=managed,
                sync=sync,
                preserve_result=sync,
            )

        return dispatch(0, args)

    async def flush(self) -> None:
        await self._runtime.flush()

    async def wait_ready(self, *, timeout: float | None = 30.0) -> None:
        """Settle lifecycle work and require every desired plugin to be active.

        Stable missing dependencies fail immediately. A timeout or caller
        cancellation leaves initialization running, allowing a later retry.
        """
        _validate_timeout(timeout)
        self._runtime.assert_can_wait_for(None)
        if self._runtime.root._close_task is not None:
            raise InactiveContextError("cannot wait for readiness during shutdown")
        deadline = asyncio.timeout(timeout)
        try:
            async with deadline:
                await self.flush()
        except TimeoutError as error:
            if not deadline.expired():
                raise
            raise ReadinessTimeoutError(self._runtime.readiness_issues()) from error
        if self._runtime.root._close_task is not None:
            raise InactiveContextError("cannot wait for readiness during shutdown")
        if issues := self._runtime.readiness_issues():
            raise ReadinessError(issues, self._runtime.dependency_cycles())

    async def close(self, *, timeout: float | None = 30.0) -> None:
        """Wait up to timeout seconds for shutdown, without cancelling cleanup.

        On expiry, ShutdownTimeoutError identifies unfinished owners and tasks.
        Repeated calls await the same shutdown. None waits without a deadline.
        """
        _validate_timeout(timeout)
        self._runtime.assert_can_wait_for(None)
        root = self._runtime.root
        task = root._close_task
        if task is None:
            task = asyncio.create_task(root._close(), name="cordis:close")
            root._close_task = task
            # A timed-out/cancelled waiter may never return. Observe eventual
            # errors here; the task still retains its result for later callers.
            task.add_done_callback(
                lambda done: None if done.cancelled() else done.exception()
            )
        if timeout is None:
            await asyncio.shield(task)
        else:
            await asyncio.wait((task,), timeout=timeout)
            if not task.done():
                raise ShutdownTimeoutError(self._runtime.shutdown_blockers())
            task.result()

    async def _close(self) -> None:
        self._closed = True
        root = self._runtime.root._fiber
        root._set_state(FiberState.UNLOADING)
        for fiber in self._runtime.fibers.copy():
            fiber.refresh()
        await self._runtime.flush()
        try:
            await _dispose_actions(root._scope, "root context")
        except Exception as error:
            self._runtime.report(error, root)
            raise
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
