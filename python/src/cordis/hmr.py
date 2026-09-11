from __future__ import annotations

import ast
import asyncio
import importlib
import importlib.util
import sys
from collections.abc import Iterable
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING, Generic

from .core import ContextT, FiberState

if TYPE_CHECKING:
    from .loader import Loader


class FullRestartRequired(RuntimeError):
    pass


def _source_path(module: ModuleType) -> Path | None:
    filename = getattr(module, "__file__", None)
    if not filename:
        return None
    try:
        if filename.endswith((".pyc", ".pyo")):
            filename = importlib.util.source_from_cache(filename)
        path = Path(filename).resolve()
        return path if path.suffix == ".py" and path.exists() else None
    except (OSError, ValueError):
        return None


class ModuleGraph:
    """A source-level import graph for currently loaded Python modules."""

    def __init__(self) -> None:
        self.paths: dict[str, Path] = {}
        self.by_path: dict[Path, set[str]] = {}
        for name, module in list(sys.modules.items()):
            if (
                not isinstance(module, ModuleType)
                or (path := _source_path(module)) is None
            ):
                continue
            self.paths[name] = path
            self.by_path.setdefault(path, set()).add(name)
        self.imports = {
            name: self._imports(name, path) for name, path in self.paths.items()
        }

    def _imports(self, name: str, path: Path) -> set[str]:
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (OSError, SyntaxError, UnicodeError):
            return set()
        package = name if path.name == "__init__.py" else name.rpartition(".")[0]
        result: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                result.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    relative = "." * node.level + (node.module or "")
                    try:
                        base = importlib.util.resolve_name(relative, package)
                    except (ImportError, ValueError):
                        continue
                else:
                    base = node.module or ""
                if base:
                    result.add(base)
                    for alias in node.names:
                        child = f"{base}.{alias.name}"
                        if child in self.paths:
                            result.add(child)
        return {dependency for dependency in result if dependency in self.paths}

    def modules_for(self, changed: Iterable[str | Path]) -> set[str]:
        result: set[str] = set()
        for item in changed:
            text = str(item)
            if text in self.paths:
                result.add(text)
                continue
            try:
                path = Path(item).resolve()
            except (OSError, TypeError):
                continue
            result.update(self.by_path.get(path, ()))
        return result

    def affected(self, changed: set[str]) -> set[str]:
        accepted = set(changed)
        while True:
            dependents = {
                name
                for name, dependencies in self.imports.items()
                if dependencies.intersection(accepted)
            }
            if dependents <= accepted:
                return accepted
            accepted.update(dependents)


class HotReloader(Generic[ContextT]):
    """Dependency-aware, transactional module reload for Loader entries."""

    def __init__(self, loader: "Loader[ContextT]", *, externals: Iterable[str] = ()) -> None:
        self.loader = loader
        self.externals = set(externals)

    async def reload_changed(self, changed: Iterable[str | Path]) -> tuple[str, ...]:
        graph = await asyncio.to_thread(ModuleGraph)
        changed_modules = await asyncio.to_thread(graph.modules_for, tuple(changed))
        if not changed_modules:
            return ()
        accepted = graph.affected(changed_modules)
        blocked = accepted.intersection(self.externals)
        if blocked:
            raise FullRestartRequired(
                "changed modules cross the HMR boundary: " + ", ".join(sorted(blocked))
            )

        stale = [
            entry_id
            for entry_id, loaded in self.loader._loaded.items()
            if isinstance(loaded.entry.plugin, str)
            and loaded.entry.plugin.partition(":")[0] in accepted
        ]
        if not stale:
            return ()
        await self.reload_entries(stale, accepted=accepted)
        return tuple(stale)

    async def reload_entries(
        self,
        entry_ids: Iterable[str],
        *,
        accepted: set[str] | None = None,
    ) -> None:
        entry_ids = list(dict.fromkeys(entry_ids))
        loaded_items = [self.loader._loaded[entry_id] for entry_id in entry_ids]
        roots = {
            loaded.entry.plugin.partition(":")[0]
            for loaded in loaded_items
            if isinstance(loaded.entry.plugin, str)
        }
        accepted = set(accepted or roots)
        backup_modules: dict[str, ModuleType] = {
            name: module
            for name in accepted
            if isinstance((module := sys.modules.get(name)), ModuleType)
        }
        old_components = {
            entry_id: self.loader._loaded[entry_id].component for entry_id in entry_ids
        }

        await asyncio.to_thread(importlib.invalidate_caches)
        cached_files = [
            Path(cached)
            for module in backup_modules.values()
            if (cached := getattr(module, "__cached__", None))
        ]
        await asyncio.gather(
            *(asyncio.to_thread(path.unlink, missing_ok=True) for path in cached_files)
        )
        for name in sorted(accepted, key=lambda value: value.count("."), reverse=True):
            sys.modules.pop(name, None)

        try:
            replacements = {
                entry_id: await asyncio.to_thread(
                    self.loader._component,
                    self.loader._loaded[entry_id].entry,
                )
                for entry_id in entry_ids
            }
        except Exception:
            self._restore_modules(accepted, backup_modules)
            raise

        try:
            await asyncio.gather(
                *(self.loader._dispose(loaded) for loaded in loaded_items)
            )
            for entry_id, component in replacements.items():
                loaded = self.loader._loaded[entry_id]
                if loaded.entry.enabled:
                    await self.loader._start(loaded, component)
            await self.loader.ctx.flush()
            failures = [
                loaded.fiber.error
                for loaded in loaded_items
                if loaded.fiber is not None
                and loaded.fiber.state is FiberState.FAILED
                and loaded.fiber.error is not None
            ]
            if failures:
                raise ExceptionGroup("hot reload failed", failures)
        except Exception as reload_error:
            await asyncio.gather(
                *(self.loader._dispose(loaded) for loaded in loaded_items),
                return_exceptions=True,
            )
            self._restore_modules(accepted, backup_modules)
            rollback_errors: list[Exception] = []
            for entry_id in entry_ids:
                loaded = self.loader._loaded[entry_id]
                if not loaded.entry.enabled:
                    continue
                try:
                    await self.loader._start(loaded, old_components[entry_id])
                except Exception as error:
                    rollback_errors.append(error)
            await self.loader.ctx.flush()
            rollback_errors.extend(
                loaded.fiber.error
                for loaded in loaded_items
                if loaded.fiber is not None
                and loaded.fiber.state is FiberState.FAILED
                and loaded.fiber.error is not None
            )
            if rollback_errors:
                raise ExceptionGroup(
                    "hot reload and rollback both failed",
                    [reload_error, *rollback_errors],
                ) from reload_error
            raise reload_error

    @staticmethod
    def _restore_modules(accepted: set[str], backup: dict[str, ModuleType]) -> None:
        for name in accepted:
            sys.modules.pop(name, None)
        sys.modules.update(backup)

    async def watch(
        self,
        paths: Iterable[str | Path],
        *,
        interval: float = 0.5,
        stop: asyncio.Event | None = None,
    ) -> None:
        files = await asyncio.gather(
            *(asyncio.to_thread(Path(path).resolve) for path in paths)
        )
        stats = await asyncio.gather(*(asyncio.to_thread(path.stat) for path in files))
        modified = {path: stat.st_mtime_ns for path, stat in zip(files, stats)}
        while stop is None or not stop.is_set():
            await asyncio.sleep(interval)
            stats = await asyncio.gather(
                *(asyncio.to_thread(path.stat) for path in files)
            )
            changed: list[Path] = []
            for path, stat in zip(files, stats):
                current = stat.st_mtime_ns
                if current != modified[path]:
                    modified[path] = current
                    changed.append(path)
            if changed:
                await self.reload_changed(changed)
