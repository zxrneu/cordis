from __future__ import annotations

import asyncio
import contextvars
import importlib
import json
import tomllib
import weakref
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Generic, TypeAlias, TypeVar, cast

from .core import (
    Component,
    BoundComponent,
    ConfigT,
    Context,
    ContextCov,
    ContextT,
    EffectHandle,
    Fiber,
    FiberState,
    FiberView,
    InactiveContextError,
    Injection,
    Plugin,
    PluginCallback,
    _as_component,
    _await,
    _settle,
    _normalize_inject,
)
from .import_tracing import trace_imports

if TYPE_CHECKING:
    from .config import ConfigStore


Isolation = Mapping[str, bool | str]
T = TypeVar("T")
_PluginLike: TypeAlias = (
    Component[Any, Any] | PluginCallback[Any, Any] | Plugin[Any, Any]
)


@dataclass
class _Mutation:
    active: bool = True


_mutations: contextvars.ContextVar[tuple[tuple[object, _Mutation], ...]] = (
    contextvars.ContextVar("cordis_loader_mutations", default=())
)


def _freeze_mapping(value: Mapping[str, object] | None) -> Mapping[str, object]:
    return MappingProxyType(dict(value or {}))


@dataclass(frozen=True, slots=True)
class Entry:
    id: str
    plugin: str | _PluginLike | None = None
    config: object = None
    enabled: bool = True
    isolate: Isolation | tuple[str, ...] = field(default_factory=dict)
    intercept: Mapping[str, object] = field(default_factory=dict)
    children: tuple["Entry | Mapping[str, Any]", ...] = ()
    inject: Injection = ()

    def __post_init__(self) -> None:
        if not isinstance(self.id, str) or not self.id:
            raise ValueError("entry id must be a non-empty string")
        if not isinstance(self.enabled, bool):
            raise TypeError("entry enabled must be a boolean")
        isolate = (
            dict.fromkeys(self.isolate, True)
            if not isinstance(self.isolate, Mapping)
            else dict(self.isolate)
        )
        for name, label in isolate.items():
            if not isinstance(name, str) or not isinstance(label, (bool, str)):
                raise TypeError(f"invalid isolate setting: {name!r}: {label!r}")
        object.__setattr__(self, "isolate", MappingProxyType(isolate))
        object.__setattr__(self, "intercept", _freeze_mapping(self.intercept))
        object.__setattr__(self, "children", tuple(self.children))
        object.__setattr__(self, "inject", _normalize_inject(self.inject))
        if self.plugin is not None and self.children:
            raise ValueError(
                f"entry {self.id!r} cannot declare both plugin and children"
            )


@dataclass(slots=True)
class _Loaded(Generic[ContextT]):
    entry: Entry
    ctx: ContextT
    # Legacy Entry is the explicit dynamic plugin/config boundary. Public
    # inspection only exposes FiberView; typed mounts never enter this record.
    fiber: Fiber[Any, ContextT] | None = None
    component: Component[Any, Any] | None = None
    transitioning: bool = False


def import_plugin(locator: str) -> _PluginLike:
    module_name, separator, attribute = locator.partition(":")
    if not separator or not module_name or not attribute:
        raise ValueError("plugin locator must use 'package.module:attribute' syntax")
    with trace_imports(module_name):
        value: object = importlib.import_module(module_name)
    for part in attribute.split("."):
        value = getattr(value, part)
    # Importlib is the dynamic boundary; mounting validates the resolved plugin.
    return cast(_PluginLike, value)


def _coerce_entry(raw: Entry | Mapping[str, Any]) -> Entry:
    return raw if isinstance(raw, Entry) else Entry(**raw)


class Loader(Generic[ContextCov]):
    """Incrementally reconcile a stable-id component configuration."""

    def __init__(self, ctx: ContextCov) -> None:
        self._ctx = ctx
        self._loaded: dict[str, _Loaded[ContextCov]] = {}
        self._mounted: dict[str, FiberView[ContextCov]] = {}
        self._bindings: dict[str, BoundComponent[Any, Any]] = {}
        self._store: ConfigStore | None = None
        self._local_realms: dict[tuple[str, str], object] = {}
        self._global_realms: dict[tuple[str, str], object] = {}
        self._lock = asyncio.Lock()
        self._update_listener: EffectHandle | None = None
        self._publications: dict[str, EffectHandle] = {}
        self._ensure_listener()

    def _ensure_listener(self) -> None:
        if self._update_listener is None:
            self._update_listener = self.ctx.on(
                "internal/update", self._on_update, global_=True
            )

    async def _on_update(
        self,
        fiber: Fiber[Any, Any],
        config: object,
        no_save: bool,
        next_step: Callable[[], object],
    ) -> None:
        if no_save:
            await _await(next_step())
            return
        loaded = next(
            (item for item in self._loaded.values() if item.fiber is fiber), None
        )
        if loaded is None:
            await _await(next_step())
            return

        async def update() -> None:
            previous, previous_config = loaded.entry, fiber.config
            await _await(next_step())
            if fiber.config == previous_config:
                return
            try:
                if self._store is not None:
                    tree = self._store.tree.update(previous.id, config=fiber.config)
                    await self._store._replace(tree, True)
                else:
                    loaded.entry = replace(previous, config=fiber.config)
            except BaseException:
                await fiber._update(previous_config, rollback=False)
                raise

        if self._store is not None:
            await self._store._run(update)
        else:
            await self._run(update)

    async def _on_dispose(self, loaded: _Loaded[ContextCov]) -> None:
        if loaded.transitioning:
            return
        if self.ctx._runtime.root.fiber.state is not FiberState.ACTIVE:
            return
        if any(loader is self and state.active for loader, state in _mutations.get()):
            return
        if not loaded.entry.enabled:
            return
        if self._store is not None:
            await self._store.update(loaded.entry.id, enabled=False)
        else:
            loaded.entry = replace(loaded.entry, enabled=False)

    def walk(self) -> Iterable[Loader[ContextCov]]:
        """Visit this Loader and every mounted group/include Loader."""
        yield self
        for entry_id in tuple(self._loaded):
            child = self.child(entry_id)
            if child is not None:
                yield from child.walk()

    async def wait_ready(self) -> None:
        """Wait for queued reconciliation and all nested lifecycle work."""
        async with self._lock:
            await self.ctx.flush()

    def provide(self, name: str = "loader") -> EffectHandle:
        """Publish explicitly; intercept(name, {'await': True}) gates consumers."""

        def ready(caller: Context) -> bool:
            metadata = caller.metadata(name)
            wait = isinstance(metadata, Mapping) and metadata.get("await")
            return not wait or not any(
                fiber.state in (FiberState.LOADING, FiberState.UNLOADING)
                for loader in self.walk()
                for fiber in [
                    *(item.fiber for item in loader._loaded.values() if item.fiber),
                    *loader._mounted.values(),
                ]
            )

        handle = self.ctx.provide(name, self, check=ready)
        self._publications[name] = handle
        return handle

    async def _run(self, operation: Callable[[], Awaitable[T]]) -> T:
        if any(loader is self and state.active for loader, state in _mutations.get()):
            raise InactiveContextError("cannot recursively mutate the same Loader")
        # Check in the caller, before the operation moves into a shielded task.
        for loaded in self._loaded.values():
            if loaded.fiber is not None:
                self.ctx._runtime.assert_can_wait_for(loaded.fiber)
        for mounted in self._mounted.values():
            self.ctx._runtime.assert_can_wait_for(mounted.ctx._fiber)
        async with self._lock:

            async def run() -> T:
                state = _Mutation()
                token = _mutations.set((*_mutations.get(), (self, state)))
                try:
                    return await operation()
                finally:
                    state.active = False
                    _mutations.reset(token)
                    self.ctx.notify("loader", *self._publications)

            # Worker imports and rollback settle before releasing this lock.
            return await _settle(run(), name="cordis:loader")

    @property
    def ctx(self) -> ContextCov:
        return self._ctx

    @property
    def entries(self) -> tuple[Entry, ...]:
        return tuple(item.entry for item in self._loaded.values())

    def get(self, entry_id: str) -> FiberView[ContextCov] | None:
        if entry_id in self._mounted:
            return self._mounted[entry_id]
        if entry_id in self._loaded:
            return self._loaded[entry_id].fiber
        parent, separator, tail = entry_id.partition("/")
        nested = self.child(parent) if separator else None
        if nested is None:
            raise KeyError(entry_id)
        return nested.get(tail)

    def child(self, entry_id: str) -> Loader[ContextCov] | None:
        """Return the nested Loader owned by a mounted group entry, if any."""
        loaded = self._loaded.get(entry_id)
        if loaded is None or loaded.entry.plugin is not None or loaded.fiber is None:
            return None
        child = _group_loaders.get(loaded.fiber.ctx)
        return cast(Loader[ContextCov] | None, child)

    async def mount(
        self: Loader[ContextT],
        entry_id: str,
        binding: BoundComponent[ConfigT, ContextT],
    ) -> Fiber[ConfigT, ContextT]:
        """Mount a bound plugin; return its typed handle, store only its lifecycle."""
        return await self._run(lambda: self._mount(entry_id, binding))

    async def _mount(
        self: Loader[ContextT],
        entry_id: str,
        binding: BoundComponent[ConfigT, ContextT],
    ) -> Fiber[ConfigT, ContextT]:
        self._ensure_listener()
        if entry_id in self._loaded or entry_id in self._mounted:
            raise ValueError(f"entry {entry_id!r} is already mounted")
        fiber = binding._mount(self.ctx)
        self._mounted[entry_id] = fiber
        self._bindings[entry_id] = binding
        try:
            await fiber.wait()
        except BaseException:
            if self._mounted.get(entry_id) is fiber:
                del self._mounted[entry_id]
                self._bindings.pop(entry_id, None)
            await fiber.dispose()
            raise
        return fiber

    async def unmount(self, entry_id: str) -> None:
        """Remove either a typed mount or a declarative Entry by its id."""
        await self._run(lambda: self._unmount(entry_id))

    async def _unmount(self, entry_id: str) -> None:
        if entry_id in self._mounted:
            self._bindings.pop(entry_id, None)
            await self._mounted.pop(entry_id).dispose()
        else:
            await self._dispose(self._loaded.pop(entry_id))

    def _realms(self, entry: Entry) -> dict[str, object]:
        result: dict[str, object] = {}
        assert isinstance(entry.isolate, Mapping)
        for name, label in entry.isolate.items():
            if label is True:
                result[name] = self._local_realms.setdefault((entry.id, name), object())
            elif isinstance(label, str):
                result[name] = self._global_realms.setdefault((label, name), object())
            elif label is not False:
                raise TypeError(
                    f"invalid isolate label for {entry.id}.{name}: {label!r}"
                )
        return result

    def _entry_context(self, entry: Entry) -> ContextCov:
        return self.ctx._derive(realms=self._realms(entry), intercepts=entry.intercept)

    def _component(self, entry: Entry) -> Component[Any, Any]:
        if entry.plugin is None:
            return group
        value = (
            import_plugin(entry.plugin)
            if isinstance(entry.plugin, str)
            else entry.plugin
        )
        component = _as_component(value)
        if entry.inject:
            component = replace(
                component,
                inject={**component.inject, **_normalize_inject(entry.inject)},
            )
        return component

    def _config(self, entry: Entry, ctx: Context | None = None) -> object:
        from .expressions import interpolate

        return (
            list(entry.children)
            if entry.plugin is None
            else interpolate(entry.config, ctx or self.ctx)
        )

    async def _dispose(self, loaded: _Loaded[ContextCov]) -> None:
        if loaded.fiber is None:
            return
        loaded.transitioning = True
        try:
            await loaded.fiber.dispose()
        finally:
            loaded.fiber = None
            loaded.transitioning = False

    async def _start(
        self, loaded: _Loaded[ContextCov], component: Component[Any, Any] | None = None
    ) -> Fiber[Any, ContextCov]:
        loaded.component = (
            component if component is not None else self._component(loaded.entry)
        )
        loaded.fiber = loaded.ctx.use(
            loaded.component, self._config(loaded.entry, loaded.ctx)
        )
        loaded.fiber._on_dispose = lambda: self._on_dispose(loaded)
        return loaded.fiber

    def _patch_context(self, loaded: _Loaded[ContextCov], entry: Entry) -> None:
        realms = self._realms(entry)
        if loaded.ctx._realms.local != realms:
            self.ctx._runtime.reassign_context_realms(loaded.ctx, realms)
        local_intercepts = loaded.ctx._intercepts.local
        local_intercepts.clear()
        local_intercepts.update(entry.intercept)

    async def reconcile(self, entries: Iterable[Entry | Mapping[str, Any]]) -> None:
        values = list(entries)
        await self._run(lambda: self._reconcile(values))

    async def _prepare(self, values: list[Entry]) -> dict[str, Component[Any, Any]]:
        ids = [entry.id for entry in values]
        if len(ids) != len(set(ids)):
            duplicates = sorted({value for value in ids if ids.count(value) > 1})
            raise ValueError(f"duplicate entry ids: {', '.join(duplicates)}")
        # Import and validate every candidate before removing any live entry.
        # Waiting for all imports prevents a failed sibling leaving late imports
        # mutating module state after this operation has already returned.
        enabled = [entry for entry in values if entry.enabled]
        results = await asyncio.gather(
            *(asyncio.to_thread(self._component, entry) for entry in enabled),
            return_exceptions=True,
        )
        resolved: dict[str, Component[Any, Any]] = {}
        for entry, result in zip(enabled, results):
            if isinstance(result, BaseException):
                raise result
            resolved[entry.id] = result
            if entry.plugin is None:
                await self._prepare([_coerce_entry(child) for child in entry.children])
        return resolved

    async def _reconcile(self, entries: Iterable[Entry | Mapping[str, Any]]) -> None:
        self._ensure_listener()
        values = [_coerce_entry(raw) for raw in entries]
        ids = [entry.id for entry in values]
        if len(ids) != len(set(ids)):
            duplicates = sorted(
                {entry_id for entry_id in ids if ids.count(entry_id) > 1}
            )
            raise ValueError(f"duplicate entry ids: {', '.join(duplicates)}")
        desired = {entry.id: entry for entry in values}
        if conflicts := self._mounted.keys() & desired.keys():
            raise ValueError(
                "entry ids already mounted: " + ", ".join(sorted(conflicts))
            )

        resolved = await self._prepare(values)
        prepared = []
        for entry in values:
            previous = self._loaded.get(entry.id)
            component = resolved.get(entry.id)
            if (
                component is not None
                and component.validate is not None
                and (previous is None or previous.entry != entry)
            ):
                config = await _await(
                    component.validate(self._config(entry, self._entry_context(entry)))
                )
                if not entry.children:
                    entry = replace(entry, config=config)
            prepared.append(entry)
        # Keep the last successfully applied Entry, not an uncommitted request.
        before = dict(self._loaded)
        snapshots = {
            key: (
                item.entry,
                item.component,
                item.fiber is not None,
                dict(item.ctx._realms.local),
                dict(item.ctx._intercepts.local),
            )
            for key, item in before.items()
        }
        touched: set[str] = set()
        previous_failures = self._failures(before)
        try:
            await self._apply(prepared, resolved, touched)
            if self.ctx.fiber.state is FiberState.ACTIVE:
                failures = []
                for key in self._loaded:
                    new_failures = [
                        failure
                        for failure in self._failures([key])
                        if not any(
                            failure is previous for previous in previous_failures
                        )
                    ]
                    if new_failures:
                        touched.add(key)
                        failures.extend(new_failures)
                if len(failures) == 1:
                    raise failures[0]
                if failures:
                    raise ExceptionGroup("loader activation failed", failures)
        except (Exception, asyncio.CancelledError) as error:
            rollback_failures: list[BaseException] = []
            for key in touched:
                item = self._loaded.pop(key, None)
                if item is not None:
                    try:
                        await self._dispose(item)
                    except (Exception, asyncio.CancelledError) as cleanup_error:
                        rollback_failures.append(cleanup_error)
            self._loaded = before
            for key, item in before.items():
                if key not in touched:
                    continue
                entry, component, mounted, realms, intercepts = snapshots[key]
                item.entry = entry
                item.component = component
                local_realms = item.ctx._realms.local
                local_realms.clear()
                local_realms.update(realms)
                local_intercepts = item.ctx._intercepts.local
                local_intercepts.clear()
                local_intercepts.update(intercepts)
                try:
                    if mounted:
                        await self._start(item, component)
                except (Exception, asyncio.CancelledError) as rollback_error:
                    rollback_failures.append(rollback_error)
            if self.ctx.fiber.state is FiberState.ACTIVE:
                await self.ctx.flush()
                rollback_failures.extend(self._failures(touched))
            if rollback_failures:
                raise BaseExceptionGroup(
                    "loader update and rollback failed", [error, *rollback_failures]
                ) from error
            raise

    async def _apply(
        self,
        values: list[Entry],
        resolved: dict[str, Component[Any, Any]],
        touched: set[str],
    ) -> None:
        desired = {entry.id for entry in values}
        for entry_id in set(self._loaded) - desired:
            touched.add(entry_id)
            await self._dispose(self._loaded.pop(entry_id))

        for entry in values:
            loaded = self._loaded.get(entry.id)
            if loaded is not None and loaded.entry == entry:
                continue
            touched.add(entry.id)
            if loaded is None:
                loaded = _Loaded(entry, self._entry_context(entry))
                self._loaded[entry.id] = loaded
                if entry.enabled:
                    await self._start(loaded, resolved.get(entry.id))
                continue

            previous = loaded.entry
            self._patch_context(loaded, entry)
            if not entry.enabled:
                await self._dispose(loaded)
                loaded.entry = entry
                continue
            if loaded.fiber is None:
                loaded.entry = entry
                await self._start(loaded, resolved.get(entry.id))
                continue
            if previous.plugin != entry.plugin or previous.inject != entry.inject:
                await self._dispose(loaded)
                loaded.entry = entry
                await self._start(loaded, resolved.get(entry.id))
                continue
            config = self._config(entry, loaded.ctx)
            if self._config(previous, loaded.ctx) != config:
                await loaded.fiber.update(config, persist=False)
            loaded.entry = entry

        if self.ctx.fiber.state is FiberState.ACTIVE:
            await self.ctx.flush()

    def _failures(self, entry_ids: Iterable[str]) -> list[Exception]:
        failures: list[Exception] = []
        for entry_id in entry_ids:
            loaded = self._loaded.get(entry_id)
            if loaded is None or loaded.fiber is None:
                continue
            if loaded.fiber.error is not None:
                failures.append(loaded.fiber.error)
            nested = _group_loaders.get(loaded.fiber.ctx)
            if nested is not None:
                failures.extend(nested._failures(nested._loaded))
        return failures

    async def reload(self, entry_id: str) -> FiberView[ContextCov]:
        return await self._run(lambda: self._reload(entry_id))

    async def _reload(self, entry_id: str) -> FiberView[ContextCov]:
        if (
            entry_id not in self._loaded
            and entry_id not in self._mounted
            and "/" in entry_id
        ):
            parent, _, tail = entry_id.partition("/")
            child = self.child(parent)
            if child is None:
                raise KeyError(entry_id)
            return await child.reload(tail)
        if entry_id in self._mounted:
            mounted = self._mounted[entry_id]
            await mounted.restart()
            return mounted
        loaded = self._loaded[entry_id]
        if not isinstance(loaded.entry.plugin, str):
            if loaded.fiber is None:
                return await self._start(loaded)
            await loaded.fiber.restart()
            return loaded.fiber
        from .hmr import HotReloader

        await HotReloader(self)._reload_entries([entry_id])
        fiber = self._loaded[entry_id].fiber
        if fiber is None:
            raise RuntimeError(f"entry {entry_id!r} did not start after reload")
        return fiber

    async def reload_changed(
        self,
        changed: Iterable[str | Path],
        *,
        externals: Iterable[str] = (),
    ) -> tuple[str, ...]:
        from .hmr import HotReloader

        return await HotReloader(self, externals=externals).reload_changed(changed)

    async def load_file(self, path: str | Path) -> None:
        path = Path(path)
        suffix = path.suffix.lower()
        text = await asyncio.to_thread(path.read_text, encoding="utf-8")
        if suffix == ".json":
            data = json.loads(text)
        elif suffix == ".toml":
            data = tomllib.loads(text)
            data = data.get("plugins", data)
        elif suffix in {".yaml", ".yml"}:
            try:
                yaml = importlib.import_module("yaml")
            except ImportError as error:
                raise RuntimeError(
                    "YAML configuration requires the optional 'yaml' extra"
                ) from error
            data = yaml.safe_load(text)
        else:
            raise ValueError(f"unsupported configuration format: {suffix}")
        if not isinstance(data, list):
            raise TypeError("Cordis configuration must be a list of entries")
        await self.reconcile(data)

    load_json = load_file

    async def watch_file(
        self,
        path: str | Path,
        *,
        interval: float = 0.5,
        stop: asyncio.Event | None = None,
        on_error: Callable[[Exception], None] | None = None,
        ready: asyncio.Event | None = None,
    ) -> None:
        from .watchers import watch_paths

        async for _ in watch_paths([path], interval=interval, stop=stop, ready=ready):
            try:
                await self.load_file(path)
            except Exception as error:
                if on_error is not None:
                    on_error(error)
                else:
                    self.ctx._runtime.report(error)

    async def close(self) -> None:
        await self._run(self._close)

    async def _close(self) -> None:
        results = await asyncio.gather(
            *(self._dispose(loaded) for loaded in list(self._loaded.values())),
            *(fiber.dispose() for fiber in list(self._mounted.values())),
            return_exceptions=True,
        )
        self._loaded.clear()
        self._mounted.clear()
        self._bindings.clear()
        handles = list(self._publications.values())
        self._publications.clear()
        if self._update_listener is not None:
            handles.append(self._update_listener)
            self._update_listener = None
        results.extend(
            await asyncio.gather(
                *(handle() for handle in handles), return_exceptions=True
            )
        )
        errors = [result for result in results if isinstance(result, Exception)]
        if errors:
            raise ExceptionGroup("loader cleanup failed", errors)


_group_loaders: weakref.WeakKeyDictionary[Context, Loader[Context]] = (
    weakref.WeakKeyDictionary()
)


async def _group_apply(
    ctx: Context, entries: Iterable[Entry | Mapping[str, Any]]
) -> Callable[[], Awaitable[None]]:
    loader = Loader(ctx)
    _group_loaders[ctx] = loader
    try:
        await loader.reconcile(entries)
    except BaseException:
        _group_loaders.pop(ctx, None)
        await loader.close()
        raise

    async def close() -> None:
        _group_loaders.pop(ctx, None)
        await loader.close()

    return close


async def _group_update(
    ctx: Context,
    _previous: Iterable[Entry | Mapping[str, Any]],
    entries: Iterable[Entry | Mapping[str, Any]],
) -> None:
    await _group_loaders[ctx].reconcile(entries)


group = Component(_group_apply, name="group", update=_group_update)
