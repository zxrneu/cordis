from __future__ import annotations

import builtins
import importlib
import importlib.util
import inspect
import sys
import threading
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from types import ModuleType
from typing import Any, cast


_Import = Any
_enabled: ContextVar[bool] = ContextVar("cordis_import_trace_enabled", default=False)
_owner: ContextVar[str | None] = ContextVar("cordis_import_trace_owner", default=None)


class RuntimeImportGraph:
    """Thread-safe runtime import edges captured during plugin loading."""

    def __init__(self) -> None:
        self._edges: dict[str, set[str]] = {}
        self._lock = threading.RLock()

    def record(self, source: str | None, dependencies: set[str]) -> None:
        if not source or not dependencies:
            return
        dependencies.discard(source)
        with self._lock:
            self._edges.setdefault(source, set()).update(dependencies)

    def clear(self, source: str) -> None:
        with self._lock:
            self._edges.pop(source, None)

    def replace(self, source: str, dependencies: set[str]) -> None:
        with self._lock:
            if dependencies:
                self._edges[source] = set(dependencies)
            else:
                self._edges.pop(source, None)

    def snapshot(self) -> Mapping[str, frozenset[str]]:
        with self._lock:
            return {
                source: frozenset(dependencies)
                for source, dependencies in self._edges.items()
            }


runtime_imports = RuntimeImportGraph()

_guard = threading.RLock()
_active = 0
_original_import: _Import = builtins.__import__
_original_import_module: _Import = importlib.import_module


def _source(globals: Mapping[str, object] | None = None) -> str | None:
    if globals is not None and isinstance(globals.get("__name__"), str):
        return cast(str, globals["__name__"])
    frame = inspect.currentframe()
    try:
        frame = frame.f_back if frame is not None else None
        while frame is not None:
            name = frame.f_globals.get("__name__")
            if name != __name__ and isinstance(name, str):
                return name
            frame = frame.f_back
    finally:
        del frame
    return _owner.get()


def _resolve(
    name: str,
    globals: Mapping[str, object] | None,
    level: int,
) -> str:
    if level == 0:
        return name
    package = globals.get("__package__") if globals is not None else None
    if not isinstance(package, str) or not package:
        return name
    try:
        return importlib.util.resolve_name("." * level + name, package)
    except (ImportError, ValueError):
        return name


def _dependencies(name: str, fromlist: object) -> set[str]:
    result = {name} if name in sys.modules else set()
    if isinstance(fromlist, (list, tuple)):
        for item in fromlist:
            if isinstance(item, str) and f"{name}.{item}" in sys.modules:
                result.add(f"{name}.{item}")
    return result


def _traced_import(
    name: str,
    globals: Mapping[str, object] | None = None,
    locals: Mapping[str, object] | None = None,
    fromlist: object = (),
    level: int = 0,
) -> ModuleType:
    result = _original_import(name, globals, locals, fromlist, level)
    if _enabled.get():
        resolved = _resolve(name, globals, level)
        runtime_imports.record(_source(globals), _dependencies(resolved, fromlist))
    return cast(ModuleType, result)


def _traced_import_module(name: str, package: str | None = None) -> ModuleType:
    # The outer loader call originates in cordis.loader, but it semantically
    # belongs to the plugin being evaluated. Dynamic imports issued by that
    # plugin share the same owner and therefore form a safe transitive edge.
    source = _owner.get()
    result = _original_import_module(name, package)
    if _enabled.get():
        try:
            resolved = (
                importlib.util.resolve_name(name, package)
                if name.startswith(".")
                else name
            )
        except (ImportError, ValueError):
            resolved = name
        runtime_imports.record(source, _dependencies(resolved, ()))
    return cast(ModuleType, result)


def _install() -> None:
    global _active
    with _guard:
        if _active == 0:
            builtins.__import__ = _traced_import
            importlib.import_module = _traced_import_module
        _active += 1


def _uninstall() -> None:
    global _active
    with _guard:
        _active -= 1
        if _active == 0:
            builtins.__import__ = _original_import
            importlib.import_module = _original_import_module


@contextmanager
def trace_imports(owner: str) -> Iterator[None]:
    """Capture imports executed while one plugin module is being evaluated."""

    previous = set(runtime_imports.snapshot().get(owner, ()))
    # Cached imports do not execute the module body and cannot rebuild its
    # dynamic edges. HMR evicts the module before an actual re-execution.
    if owner not in sys.modules:
        runtime_imports.clear(owner)
    _install()
    enabled_token = _enabled.set(True)
    owner_token = _owner.set(owner)
    try:
        yield
    except BaseException:
        runtime_imports.replace(owner, previous)
        raise
    finally:
        _owner.reset(owner_token)
        _enabled.reset(enabled_token)
        _uninstall()
