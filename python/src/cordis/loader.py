from __future__ import annotations

import asyncio
import importlib
import json
import tomllib
import weakref
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, TypeAlias, cast

from .core import (
    Component,
    Context,
    Fiber,
    FiberState,
    Plugin,
    PluginCallback,
    _as_component,
)


Isolation = Mapping[str, bool | str]
_PluginLike: TypeAlias = (
    Component[Any, Any] | PluginCallback[Any, Any] | Plugin[Any, Any]
)


def _freeze_mapping(value: Mapping[str, Any] | None) -> Mapping[str, Any]:
    return MappingProxyType(dict(value or {}))


@dataclass(frozen=True, slots=True)
class Entry:
    id: str
    plugin: str | _PluginLike | None = None
    config: object = None
    enabled: bool = True
    isolate: Isolation | tuple[str, ...] = field(default_factory=dict)
    intercept: Mapping[str, Any] = field(default_factory=dict)
    children: tuple["Entry | Mapping[str, Any]", ...] = ()

    def __post_init__(self) -> None:
        isolate = (
            dict.fromkeys(self.isolate, True)
            if not isinstance(self.isolate, Mapping)
            else dict(self.isolate)
        )
        object.__setattr__(self, "isolate", MappingProxyType(isolate))
        object.__setattr__(self, "intercept", _freeze_mapping(self.intercept))
        object.__setattr__(self, "children", tuple(self.children))
        if self.plugin is None and not self.children:
            raise ValueError(f"entry {self.id!r} needs a plugin or children")
        if self.plugin is not None and self.children:
            raise ValueError(
                f"entry {self.id!r} cannot declare both plugin and children"
            )


@dataclass(slots=True)
class _Loaded:
    entry: Entry
    ctx: Context
    fiber: Fiber[Any, Any] | None = None
    component: Component[Any, Any] | None = None


def import_plugin(locator: str) -> _PluginLike:
    module_name, separator, attribute = locator.partition(":")
    if not separator or not module_name or not attribute:
        raise ValueError("plugin locator must use 'package.module:attribute' syntax")
    value: Any = importlib.import_module(module_name)
    for part in attribute.split("."):
        value = getattr(value, part)
    # Importlib is the dynamic boundary; mounting validates the resolved plugin.
    return cast(_PluginLike, value)


def _coerce_entry(raw: Entry | Mapping[str, Any]) -> Entry:
    return raw if isinstance(raw, Entry) else Entry(**raw)


class Loader:
    """Incrementally reconcile a stable-id component configuration."""

    def __init__(self, ctx: Context) -> None:
        self.ctx = ctx
        self._loaded: dict[str, _Loaded] = {}
        self._local_realms: dict[tuple[str, str], object] = {}
        self._global_realms: dict[tuple[str, str], object] = {}

    @property
    def entries(self) -> tuple[Entry, ...]:
        return tuple(item.entry for item in self._loaded.values())

    def get(self, entry_id: str) -> Fiber[Any, Any] | None:
        return self._loaded[entry_id].fiber

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

    def _entry_context(self, entry: Entry) -> Context:
        return self.ctx._derive(realms=self._realms(entry), intercepts=entry.intercept)

    def _component(self, entry: Entry) -> Component[Any, Any]:
        if entry.children:
            return group
        if entry.plugin is None:
            raise ValueError(f"entry {entry.id!r} needs a plugin")
        value = (
            import_plugin(entry.plugin)
            if isinstance(entry.plugin, str)
            else entry.plugin
        )
        return _as_component(value)

    def _config(self, entry: Entry) -> object:
        return list(entry.children) if entry.children else entry.config

    async def _dispose(self, loaded: _Loaded) -> None:
        if loaded.fiber is None:
            return
        try:
            await loaded.fiber.dispose()
        finally:
            loaded.fiber = None

    async def _start(
        self, loaded: _Loaded, component: Component[Any, Any] | None = None
    ) -> Fiber[Any, Any]:
        loaded.component = (
            component if component is not None else self._component(loaded.entry)
        )
        loaded.fiber = loaded.ctx.use(loaded.component, self._config(loaded.entry))
        return loaded.fiber

    def _patch_context(self, loaded: _Loaded, entry: Entry) -> None:
        realms = self._realms(entry)
        if loaded.ctx._realms_local != realms:
            self.ctx._runtime.reassign_context_realms(loaded.ctx, realms)
        loaded.ctx._intercepts_local.clear()
        loaded.ctx._intercepts_local.update(entry.intercept)

    async def reconcile(self, entries: Iterable[Entry | Mapping[str, Any]]) -> None:
        values = [_coerce_entry(raw) for raw in entries]
        ids = [entry.id for entry in values]
        if len(ids) != len(set(ids)):
            duplicates = sorted(
                {entry_id for entry_id in ids if ids.count(entry_id) > 1}
            )
            raise ValueError(f"duplicate entry ids: {', '.join(duplicates)}")
        desired = {entry.id: entry for entry in values}

        for entry_id in set(self._loaded) - set(desired):
            await self._dispose(self._loaded.pop(entry_id))

        imports = {
            entry.id: asyncio.to_thread(self._component, entry)
            for entry in values
            if entry.enabled
            and (
                entry.id not in self._loaded
                or self._loaded[entry.id].entry.plugin != entry.plugin
                or self._loaded[entry.id].component is None
            )
        }
        resolved = (
            dict(zip(imports, await asyncio.gather(*imports.values())))
            if imports
            else {}
        )

        for entry in values:
            loaded = self._loaded.get(entry.id)
            if loaded is None:
                loaded = _Loaded(entry, self._entry_context(entry))
                self._loaded[entry.id] = loaded
                if entry.enabled:
                    await self._start(loaded, resolved.get(entry.id))
                continue

            previous = loaded.entry
            self._patch_context(loaded, entry)
            loaded.entry = entry
            if not entry.enabled:
                await self._dispose(loaded)
                continue
            if loaded.fiber is None:
                await self._start(loaded, resolved.get(entry.id))
                continue
            if previous.plugin != entry.plugin or bool(previous.children) != bool(
                entry.children
            ):
                await self._dispose(loaded)
                await self._start(loaded, resolved.get(entry.id))
                continue
            config = self._config(entry)
            if self._config(previous) != config:
                await loaded.fiber.update(config)

        if self.ctx.fiber.state is FiberState.ACTIVE:
            await self.ctx.flush()

    async def reload(self, entry_id: str) -> Fiber[Any, Any]:
        loaded = self._loaded[entry_id]
        if not isinstance(loaded.entry.plugin, str):
            if loaded.fiber is None:
                return await self._start(loaded)
            await loaded.fiber.restart()
            return loaded.fiber
        from .hmr import HotReloader

        await HotReloader(self).reload_entries([entry_id])
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
    ) -> None:
        path = Path(path)
        modified = (await asyncio.to_thread(path.stat)).st_mtime_ns
        while stop is None or not stop.is_set():
            await asyncio.sleep(interval)
            current = (await asyncio.to_thread(path.stat)).st_mtime_ns
            if current == modified:
                continue
            modified = current
            await self.load_file(path)

    async def close(self) -> None:
        results = await asyncio.gather(
            *(self._dispose(loaded) for loaded in list(self._loaded.values())),
            return_exceptions=True,
        )
        self._loaded.clear()
        errors = [result for result in results if isinstance(result, Exception)]
        if errors:
            raise ExceptionGroup("loader cleanup failed", errors)


_group_loaders: weakref.WeakKeyDictionary[Context, Loader] = weakref.WeakKeyDictionary()


async def _group_apply(
    ctx: Context, entries: Iterable[Entry | Mapping[str, Any]]
) -> Callable[[], Awaitable[None]]:
    loader = Loader(ctx)
    _group_loaders[ctx] = loader
    await loader.reconcile(entries)

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
