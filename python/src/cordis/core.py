from __future__ import annotations

import asyncio
import contextvars
import inspect
from collections import defaultdict
from collections.abc import AsyncIterable, Callable, Iterable, Mapping, MutableSequence
from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Any, TypeAlias


Cleanup: TypeAlias = Callable[[], Any]
PluginCallback: TypeAlias = Callable[["Context", Any], Any]
Injection: TypeAlias = Iterable[str] | Mapping[str, Any]
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


class MissingProvisionError(CordisError):
    pass


class FiberState(str, Enum):
    PENDING = "pending"
    LOADING = "loading"
    ACTIVE = "active"
    FAILED = "failed"
    UNLOADING = "unloading"
    DISPOSED = "disposed"


def _normalize_inject(value: Injection | None) -> Mapping[str, Any]:
    if value is None:
        return MappingProxyType({})
    if isinstance(value, Mapping):
        return MappingProxyType(dict(value))
    return MappingProxyType(dict.fromkeys(value))


@dataclass(frozen=True, slots=True)
class Component:
    apply: PluginCallback
    inject: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))
    provide: frozenset[str] = frozenset()
    name: str | None = None
    validate: Callable[[Any], Any] | None = None
    update: Callable[["Context", Any, Any], Any] | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "inject", _normalize_inject(self.inject))
        object.__setattr__(self, "provide", frozenset(self.provide))


def plugin(
    target: PluginCallback | None = None,
    *,
    inject: Injection = (),
    provide: Iterable[str] = (),
    name: str | None = None,
    validate: Callable[[Any], Any] | None = None,
    update: Callable[["Context", Any, Any], Any] | None = None,
):
    """Declare a function or class as a Cordis component."""

    def wrap(callback: PluginCallback) -> Component:
        return Component(
            callback,
            _normalize_inject(inject),
            frozenset(provide),
            name or callback.__name__,
            validate,
            update,
        )

    return wrap(target) if target is not None else wrap


component = plugin


@dataclass(slots=True)
class _Service:
    name: str
    value: Any
    fiber: "Fiber"
    realm: object
    check: Callable[["Context"], bool] | None = None
    adapt: Callable[[Any, Any, "Context"], Any] | None = None


@dataclass(slots=True)
class _Listener:
    ctx: "Context"
    callback: Callable[..., Any]
    global_: bool = False


@dataclass(slots=True)
class _Sink:
    fiber: "Fiber"
    token: object
    actions: MutableSequence[Any]


_collector: contextvars.ContextVar[_Sink | None] = contextvars.ContextVar(
    "cordis_effect_collector", default=None
)


async def _await(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


async def _dispose_actions(actions: MutableSequence[Any], label: str) -> None:
    errors: list[Exception] = []
    while actions:
        action = actions.pop()
        try:
            if isinstance(action, _Effect):
                await action.dispose()
            else:
                await _await(action())
        except Exception as error:
            errors.append(error)
    if errors:
        raise ExceptionGroup(f"errors while disposing {label}", errors)


def _collect_value(value: Any, sink: _Sink) -> None:
    if value is None:
        return
    if isinstance(value, EffectHandle):
        value._effect.attach(sink.actions)
        return
    if callable(value):
        sink.actions.append(value)
        return
    raise TypeError("an effect must return or yield cleanup callables")


async def _execute(
    callback: Callable[[], Any], guard: Callable[[], bool], sink: _Sink
) -> None:
    token = _collector.set(sink)
    iterator: Any = None
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
                    break
                _collect_value(item, sink)
            return

        if isinstance(value, Iterable) and not isinstance(value, (str, bytes, Mapping)):
            iterator = iter(value)
            while guard():
                try:
                    item = next(iterator)
                except StopIteration:
                    break
                _collect_value(item, sink)
            return

        _collect_value(value, sink)
    finally:
        if iterator is not None and not guard():
            close = getattr(iterator, "aclose", None) or getattr(
                iterator, "close", None
            )
            if close:
                await _await(close())
        _collector.reset(token)


class EffectHandle:
    """An idempotent cleanup handle returned by context operations."""

    def __init__(self, effect: "_Effect"):
        self._effect = effect

    async def __call__(self) -> None:
        await self._effect.dispose()


class _Effect:
    def __init__(self, fiber: "Fiber", label: str):
        self.fiber = fiber
        self.label = label
        self.actions: list[Any] = []
        self.armed = True
        self.parent: MutableSequence[Any] | None = None
        self.task: asyncio.Task[None] | None = None
        self.error: Exception | None = None
        self.handle = EffectHandle(self)

    def attach(self, parent: MutableSequence[Any]) -> None:
        if self.parent is parent:
            return
        if self.parent is not None and self in self.parent:
            self.parent.remove(self)
        self.parent = parent
        parent.append(self)

    @classmethod
    def completed(
        cls, fiber: "Fiber", cleanups: Iterable[Cleanup], label: str
    ) -> "_Effect":
        effect = cls(fiber, label)
        effect.actions.extend(cleanups)
        return effect

    def start(self, callback: Callable[[], Any]) -> None:
        sink = _Sink(self.fiber, self.fiber._load_token, self.actions)
        self.task = asyncio.create_task(
            self._run(callback, sink), name=f"cordis:effect:{self.label}"
        )

    async def _run(self, callback: Callable[[], Any], sink: _Sink) -> None:
        try:
            await _execute(callback, lambda: self.armed, sink)
        except Exception as error:
            self.armed = False
            try:
                await _dispose_actions(self.actions, self.label)
            except ExceptionGroup as cleanup_error:
                error = ExceptionGroup(
                    f"effect {self.label!r} failed and rollback also failed",
                    [error, cleanup_error],
                )
            self.error = error
            self.detach()
            raise error

    def detach(self) -> None:
        if self.parent is not None and self in self.parent:
            self.parent.remove(self)
        self.parent = None

    async def wait(self) -> EffectHandle:
        if self.task:
            await asyncio.shield(self.task)
        return self.handle

    async def dispose(self) -> None:
        if not self.armed and not self.actions:
            self.detach()
            return
        self.armed = False
        if self.task:
            try:
                await asyncio.shield(self.task)
            except Exception:
                self.detach()
                return
        try:
            await _dispose_actions(self.actions, self.label)
        finally:
            self.detach()


class Fiber:
    def __init__(
        self,
        runtime: "_Runtime",
        parent: "Context",
        component: Component | None,
        config: Any = None,
        *,
        root: bool = False,
    ):
        self.runtime = runtime
        self.parent = parent
        self.component = component
        self.config = config
        self.uid: int | None = 0 if root else runtime.next_uid()
        self.name = "root" if root else component.name or f"component-{self.uid}"
        self.state = FiberState.ACTIVE if root else FiberState.PENDING
        self.desired = True
        self.revision = 0
        self.target: tuple[int, tuple[int, ...]] | None | object = _EMPTY
        self.committed: dict[str, _Service] = {}
        self.committed_realms: dict[str, object] = {}
        self.inertia: asyncio.Task[None] | None = None
        self.error: Exception | None = None
        self.cleanup_error: ExceptionGroup | None = None
        self._actions: list[Any] = []
        self._load_token = object()
        intercepts = {
            key: metadata
            for key, metadata in (component.inject.items() if component else ())
            if metadata is not None
        }
        self.ctx = parent if root else parent._derive(fiber=self, intercepts=intercepts)

    @property
    def inject(self) -> Mapping[str, Any]:
        return self.component.inject if self.component else MappingProxyType({})

    def _assert_effects_allowed(self) -> None:
        if self.state not in (FiberState.LOADING, FiberState.ACTIVE):
            raise InactiveContextError(
                f"cannot create effect in {self.state.value} component {self.name!r}"
            )

    def _sink(self) -> MutableSequence[Any]:
        current = _collector.get()
        if current is not None and current.fiber is self:
            if current.token is not self._load_token:
                raise InactiveContextError(
                    f"stale task attempted an effect in component {self.name!r}"
                )
            return current.actions
        return self._actions

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

    async def _validate_config(self, config: Any) -> Any:
        if self.component is None or self.component.validate is None:
            return config
        return await _await(self.component.validate(config))

    def _component_callback(self) -> Callable[[], Any]:
        callback = self.component.apply

        async def invoke_class() -> Any:
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

    def _check_provides(self) -> None:
        for name in self.component.provide:
            service = self.runtime.services.get(self.ctx._realm(name))
            if service is None or service.fiber is not self:
                raise MissingProvisionError(
                    f"component {self.name!r} declared but did not provide service {name!r}"
                )

    async def _recover(self) -> None:
        try:
            await _dispose_actions(self._actions, f"component:{self.name}")
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
                self.cleanup_error = None
                try:
                    self.config = await self._validate_config(self.config)
                    self._commit()
                    sink = _Sink(self, self._load_token, self._actions)
                    await _execute(
                        self._component_callback(),
                        lambda: self.target == target and self.desired,
                        sink,
                    )
                    self._check_provides()
                except Exception as load_error:
                    try:
                        await _dispose_actions(self._actions, f"component:{self.name}")
                    except ExceptionGroup as cleanup_error:
                        load_error = ExceptionGroup(
                            f"component {self.name!r} failed and rollback also failed",
                            [load_error, cleanup_error],
                        )
                    self.committed.clear()
                    self.committed_realms.clear()
                    self.error = load_error
                    self.target = None
                    self._set_state(FiberState.FAILED)
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

    async def update(self, config: Any, *, rollback: bool = True) -> None:
        config = await self._validate_config(config)
        if config == self.config:
            return
        previous = self.config
        if self.component.update is not None and self.state is FiberState.ACTIVE:
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

    async def wait(self, *, raise_error: bool = True) -> "Fiber":
        while self.inertia is not None:
            await asyncio.shield(self.inertia)
        if raise_error and self.error:
            raise self.error
        return self

    def __await__(self):
        return self.wait().__await__()


class _Runtime:
    def __init__(self):
        self.uid = 0
        self.fibers: list[Fiber] = []
        self.services: dict[object, _Service] = {}
        self.listeners: dict[str, list[_Listener]] = defaultdict(list)
        self.errors: list[tuple[Exception, Fiber | None]] = []
        self.event_tasks: set[asyncio.Task[Any]] = set()
        self.root: Context

    def next_uid(self) -> int:
        self.uid += 1
        return self.uid

    def add(self, fiber: Fiber) -> None:
        self.fibers.append(fiber)

    def remove(self, fiber: Fiber) -> None:
        if fiber in self.fibers:
            self.fibers.remove(fiber)
        fiber.uid = None

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

    def provided_names(self, fiber: Fiber) -> set[str]:
        return {
            service.name for service in self.services.values() if service.fiber is fiber
        }

    def notify(self, ctx: "Context", names: Iterable[str]) -> list[Fiber]:
        names = set(names)
        affected: list[Fiber] = []
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

    def dependents_of(self, provider: Fiber) -> list[Fiber]:
        result: list[Fiber] = []
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

    def provider_leaving(self, fiber: Fiber) -> None:
        names = self.provided_names(fiber)
        if names:
            self.notify(fiber.ctx, names)
        for child in self.fibers.copy():
            if child.parent._fiber is fiber:
                child.refresh()

    def provider_active(self, fiber: Fiber) -> None:
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
            for fiber in self.fibers:
                if (
                    fiber.ctx._is_descendant_of(ctx)
                    and fiber.committed.get(service.name) is service
                    and fiber.committed_realms.get(service.name) is source
                ):
                    fiber.committed_realms[service.name] = target
        for fiber in self.fibers.copy():
            fiber.refresh()

    def report(self, error: Exception, fiber: Fiber | None = None) -> None:
        self.errors.append((error, fiber))
        if self.listeners.get("internal/error"):
            self.emit("internal/error", error, fiber)

    def _callbacks(self, name: str, scope: Any = None) -> list[Callable[..., Any]]:
        callbacks: list[Callable[..., Any]] = []
        for listener in self.listeners[name].copy():
            if listener.global_ or scope is None:
                callbacks.append(listener.callback)
            elif isinstance(scope, Context) and listener.ctx._is_descendant_of(scope):
                callbacks.append(listener.callback)
            elif callable(scope) and scope(listener.ctx):
                callbacks.append(listener.callback)
        return callbacks

    def _track_event_result(self, result: Any, source: str) -> None:
        if not inspect.isawaitable(result):
            return
        task = asyncio.create_task(result)
        self.event_tasks.add(task)
        task.add_done_callback(self.event_tasks.discard)

        def report(task: asyncio.Task[Any]) -> None:
            if not task.cancelled() and (error := task.exception()):
                if source == "internal/error":
                    self.errors.append((error, None))
                else:
                    self.report(error)

        task.add_done_callback(report)

    def emit(self, name: str, *args: Any, scope: Any = None) -> None:
        for callback in self._callbacks(name, scope):
            try:
                self._track_event_result(callback(*args), name)
            except Exception as error:
                if name == "internal/error":
                    self.errors.append((error, None))
                else:
                    self.report(error)

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

    def __init__(self):
        runtime = _Runtime()
        object.__setattr__(self, "_runtime", runtime)
        object.__setattr__(self, "_parent", None)
        object.__setattr__(self, "_realms_local", {})
        object.__setattr__(self, "_intercepts_local", {})
        object.__setattr__(self, "_closed", False)
        root = Fiber(runtime, self, None, root=True)
        object.__setattr__(self, "_fiber", root)
        runtime.root = self

    def _derive(
        self,
        *,
        fiber: Fiber | None = None,
        realms: Mapping[str, object] | None = None,
        intercepts: Mapping[str, Any] | None = None,
    ) -> "Context":
        ctx = object.__new__(Context)
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
    def fiber(self) -> Fiber:
        return self._fiber

    @property
    def fibers(self) -> tuple[Fiber, ...]:
        return tuple(self._runtime.fibers)

    @property
    def errors(self) -> tuple[tuple[Exception, Fiber | None], ...]:
        return tuple(self._runtime.errors)

    def _realm(self, name: str) -> object:
        ctx: Context | None = self
        while ctx is not None:
            if name in ctx._realms_local:
                return ctx._realms_local[name]
            ctx = ctx._parent
        return self._runtime.root._realms_local.setdefault(name, object())

    def isolate(self, *names: str, realm: object | None = None) -> "Context":
        if realm is not None and len(names) != 1:
            raise ValueError("an explicit realm can isolate exactly one service")
        return self._derive(realms={name: realm or object() for name in names})

    def intercept(self, name: str, metadata: Any) -> "Context":
        return self._derive(intercepts={name: metadata})

    def metadata(self, name: str) -> Any:
        layers: list[Any] = []
        ctx: Context | None = self
        while ctx is not None:
            if name in ctx._intercepts_local:
                layers.append(ctx._intercepts_local[name])
            ctx = ctx._parent
        if not layers:
            return None
        layers.reverse()
        if all(isinstance(layer, Mapping) for layer in layers):
            merged: dict[str, Any] = {}
            for layer in layers:
                merged.update(layer)
            return merged
        return layers[-1]

    def use(self, value: Component | PluginCallback | Any, config: Any = None) -> Fiber:
        self._fiber._assert_effects_allowed()
        spec = _as_component(value)
        fiber = Fiber(self._runtime, self, spec, config)
        self._runtime.add(fiber)

        async def remove() -> None:
            await fiber.dispose()

        effect = _Effect.completed(self._fiber, [remove], f"component:{fiber.name}")
        self._fiber._attach_effect(effect)
        fiber.refresh()
        return fiber

    plugin = use

    def inject(
        self, dependencies: Injection, callback: PluginCallback, config: Any = None
    ) -> Fiber:
        return self.use(
            Component(
                callback, _normalize_inject(dependencies), name=callback.__name__
            ),
            config,
        )

    async def effect(
        self, callback: Callable[[], Any], label: str = "anonymous"
    ) -> EffectHandle:
        self._fiber._assert_effects_allowed()
        effect = _Effect(self._fiber, label)
        self._fiber._attach_effect(effect)
        effect.start(callback)
        return await effect.wait()

    def provide(
        self,
        name: str,
        value: Any,
        *,
        check: Callable[["Context"], bool] | None = None,
        adapt: Callable[[Any, Any, "Context"], Any] | None = None,
    ) -> EffectHandle:
        self._fiber._assert_effects_allowed()
        realm = self._realm(name)
        existing = self._runtime.services.get(realm)
        if existing:
            raise DuplicateServiceError(
                f"service {name!r} is already provided by component {existing.fiber.name!r}"
            )
        service = _Service(name, value, self._fiber, realm, check, adapt)
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
        return self._fiber._attach_effect(effect)

    def set(self, name: str, value: Any) -> None:
        service = self._runtime.services.get(self._realm(name))
        if service is None or service.fiber is not self._fiber:
            raise DuplicateServiceError(
                f"component {self._fiber.name!r} does not own service {name!r}"
            )
        service.value = value

    def notify(self, *names: str) -> None:
        """Re-evaluate dependents after an availability predicate changes."""
        self._runtime.notify(self, names)

    def get(
        self, name: str, default: Any = None, *, include_inactive: bool = False
    ) -> Any:
        service = self._runtime.resolve(self, name, active=not include_inactive)
        return default if service is None else self._adapt(service)

    def _adapt(self, service: _Service) -> Any:
        metadata = self.metadata(service.name)
        if service.adapt:
            return service.adapt(service.value, metadata, self)
        hook = getattr(service.value, "__cordis_intercept__", None)
        return hook(metadata, self) if hook else service.value

    def service(self, name: str) -> Any:
        realm = self._realm(name)
        fiber: Fiber | None = self._fiber
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
            if fiber.parent._realm(name) is not realm:
                break
            fiber = fiber.parent._fiber
        if self._fiber is self._runtime.root._fiber:
            service = self._runtime.resolve(self, name)
            if service:
                return self._adapt(service)
        raise UndeclaredServiceError(
            f"component {self._fiber.name!r} accessed service {name!r} without declaring it"
        )

    def __getitem__(self, name: str) -> Any:
        return self.service(name)

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        return self.service(name)

    def on(
        self,
        name: str,
        callback: Callable[..., Any],
        *,
        prepend: bool = False,
        global_: bool = False,
    ) -> EffectHandle:
        self._fiber._assert_effects_allowed()
        listener = _Listener(self, callback, global_)
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

    def once(
        self, name: str, callback: Callable[..., Any], **options: Any
    ) -> EffectHandle:
        handle: EffectHandle

        async def wrapped(*args: Any) -> Any:
            await handle()
            return await _await(callback(*args))

        handle = self.on(name, wrapped, **options)
        return handle

    def emit(self, name: str, *args: Any, scope: Any = None) -> None:
        self._runtime.emit(name, *args, scope=scope)

    async def parallel(self, name: str, *args: Any, scope: Any = None) -> None:
        async def invoke(callback: Callable[..., Any]) -> Any:
            return await _await(callback(*args))

        results = await asyncio.gather(
            *(invoke(callback) for callback in self._runtime._callbacks(name, scope)),
            return_exceptions=True,
        )
        errors = [result for result in results if isinstance(result, Exception)]
        if errors:
            raise ExceptionGroup(f"event {name!r} failed", errors)

    async def serial(self, name: str, *args: Any, scope: Any = None) -> Any:
        for callback in self._runtime._callbacks(name, scope):
            result = await _await(callback(*args))
            if result is not None and result is not False:
                return result
        return None

    def bail(self, name: str, *args: Any, scope: Any = None) -> Any:
        for callback in self._runtime._callbacks(name, scope):
            result = callback(*args)
            if inspect.isawaitable(result):
                raise TypeError("bail listeners must be synchronous")
            if result is not None and result is not False:
                return result
        return None

    def waterfall(
        self,
        name: str,
        *args: Any,
        final: Callable[..., Any],
        scope: Any = None,
    ) -> Any:
        callbacks = self._runtime._callbacks(name, scope)

        def dispatch(index: int, values: tuple[Any, ...]) -> Any:
            if index == len(callbacks):
                return final(*values)
            return callbacks[index](
                *values, lambda *next_values: dispatch(index + 1, next_values or values)
            )

        return dispatch(0, args)

    async def flush(self) -> None:
        await self._runtime.flush()

    async def close(self) -> None:
        if self._closed:
            return
        object.__setattr__(self, "_closed", True)
        root = self._runtime.root._fiber
        root._set_state(FiberState.UNLOADING)
        for fiber in self._runtime.fibers.copy():
            fiber.refresh()
        await self._runtime.flush()
        try:
            await _dispose_actions(root._actions, "root context")
        finally:
            root._set_state(FiberState.DISPOSED)

    async def __aenter__(self) -> "Context":
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.close()


def _as_component(value: Component | PluginCallback | Any) -> Component:
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
