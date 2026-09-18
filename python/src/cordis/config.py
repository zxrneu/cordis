from __future__ import annotations

import asyncio
import hashlib
import importlib
import json
import os
import tempfile
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Protocol, TypeAlias, TypeVar, cast

from .loader import Entry, Loader


EntryPath: TypeAlias = str | Sequence[str]
T = TypeVar("T")


class ConfigError(RuntimeError):
    """Base class for persistent configuration failures."""


class StaleConfigError(ConfigError):
    """The backing store changed after it was loaded."""


class ConfigCodec(Protocol):
    """Serialize a complete entry tree independently of its storage."""

    def loads(self, text: str) -> Sequence[Entry | Mapping[str, Any]]: ...

    def dumps(self, entries: Sequence[Entry]) -> str: ...


class ConfigBackend(Protocol):
    """Async storage boundary used by ConfigStore."""

    @property
    def identity(self) -> str: ...

    async def read(self) -> str | None: ...

    async def write(self, text: str) -> None: ...


def _parts(path: EntryPath) -> tuple[str, ...]:
    parts = (
        tuple(part for part in path.split("/") if part)
        if isinstance(path, str)
        else tuple(path)
    )
    if not parts or any(not isinstance(part, str) or not part for part in parts):
        raise ValueError("entry path must contain non-empty string ids")
    return parts


def _coerce_entry(value: Entry | Mapping[str, Any]) -> Entry:
    if isinstance(value, Entry):
        return value
    raw = dict(value)
    if "children" in raw:
        raw["children"] = tuple(_coerce_entry(child) for child in raw["children"])
    return Entry(**raw)


def _entry_data(entry: Entry) -> dict[str, object]:
    if entry.plugin is not None and not isinstance(entry.plugin, str):
        raise ConfigError(
            f"entry {entry.id!r} uses an in-memory plugin and cannot be persisted"
        )
    data: dict[str, object] = {"id": entry.id}
    if entry.plugin is None:
        data["children"] = [
            _entry_data(_coerce_entry(child)) for child in entry.children
        ]
    else:
        data["plugin"] = entry.plugin
        if entry.config is not None:
            data["config"] = entry.config
    if not entry.enabled:
        data["enabled"] = False
    if entry.isolate:
        assert isinstance(entry.isolate, Mapping)
        data["isolate"] = dict(entry.isolate)
    if entry.intercept:
        data["intercept"] = dict(entry.intercept)
    return data


@dataclass(frozen=True, slots=True)
class EntryTree:
    """An immutable, validated tree of declarative Loader entries."""

    entries: tuple[Entry, ...] = ()

    def __post_init__(self) -> None:
        entries = tuple(_coerce_entry(entry) for entry in self.entries)
        self._validate(entries)
        object.__setattr__(self, "entries", entries)

    @classmethod
    def from_values(cls, values: Sequence[Entry | Mapping[str, Any]]) -> EntryTree:
        return cls(tuple(_coerce_entry(value) for value in values))

    @staticmethod
    def _validate(entries: tuple[Entry, ...]) -> None:
        ids = [entry.id for entry in entries]
        if len(ids) != len(set(ids)):
            duplicate = next(value for value in ids if ids.count(value) > 1)
            raise ValueError(f"duplicate entry id {duplicate!r}")
        for entry in entries:
            EntryTree._validate(tuple(_coerce_entry(child) for child in entry.children))

    def get(self, path: EntryPath) -> Entry:
        entries = self.entries
        current: Entry | None = None
        for part in _parts(path):
            current = next((entry for entry in entries if entry.id == part), None)
            if current is None:
                raise KeyError("/".join(_parts(path)))
            entries = tuple(_coerce_entry(child) for child in current.children)
        assert current is not None
        return current

    def create(
        self,
        entry: Entry | Mapping[str, Any],
        *,
        parent: EntryPath | None = None,
        position: int | None = None,
    ) -> EntryTree:
        value = _coerce_entry(entry)
        if parent is None:
            return EntryTree(self._insert(self.entries, value, position))
        parts = _parts(parent)

        def add(group: Entry) -> Entry:
            if group.plugin is not None:
                raise ValueError(f"entry {group.id!r} is not a group")
            children = tuple(_coerce_entry(child) for child in group.children)
            return replace(group, children=self._insert(children, value, position))

        return EntryTree(self._transform(self.entries, parts, add))

    @staticmethod
    def _insert(
        entries: tuple[Entry, ...], entry: Entry, position: int | None
    ) -> tuple[Entry, ...]:
        if any(current.id == entry.id for current in entries):
            raise ValueError(f"entry {entry.id!r} already exists")
        values = list(entries)
        values.insert(len(values) if position is None else position, entry)
        return tuple(values)

    def update(
        self,
        path: EntryPath,
        entry: Entry | Mapping[str, Any] | None = None,
        **changes: object,
    ) -> EntryTree:
        if entry is not None and changes:
            raise TypeError("pass either an entry or field changes, not both")

        def update_value(current: Entry) -> Entry:
            if entry is not None:
                return _coerce_entry(entry)
            values = {
                "id": current.id,
                "plugin": current.plugin,
                "config": current.config,
                "enabled": current.enabled,
                "isolate": current.isolate,
                "intercept": current.intercept,
                "children": current.children,
                **changes,
            }
            return Entry(**cast(Any, values))

        return EntryTree(self._transform(self.entries, _parts(path), update_value))

    def remove(self, path: EntryPath) -> tuple[EntryTree, Entry]:
        parts = _parts(path)
        removed: Entry | None = None

        def visit(entries: tuple[Entry, ...], depth: int) -> tuple[Entry, ...]:
            nonlocal removed
            index = next(
                (i for i, item in enumerate(entries) if item.id == parts[depth]), -1
            )
            if index < 0:
                raise KeyError("/".join(parts))
            values = list(entries)
            current = values[index]
            if depth == len(parts) - 1:
                removed = current
                del values[index]
            else:
                children = tuple(_coerce_entry(child) for child in current.children)
                values[index] = replace(current, children=visit(children, depth + 1))
            return tuple(values)

        tree = EntryTree(visit(self.entries, 0))
        assert removed is not None
        return tree, removed

    def move(
        self,
        path: EntryPath,
        *,
        parent: EntryPath | None = None,
        position: int | None = None,
    ) -> EntryTree:
        source = _parts(path)
        target = () if parent is None else _parts(parent)
        if target[: len(source)] == source:
            raise ValueError("cannot move an entry into itself or its descendants")
        tree, entry = self.remove(source)
        return tree.create(entry, parent=target or None, position=position)

    @classmethod
    def _transform(
        cls,
        entries: tuple[Entry, ...],
        parts: tuple[str, ...],
        callback: Any,
    ) -> tuple[Entry, ...]:
        index = next((i for i, item in enumerate(entries) if item.id == parts[0]), -1)
        if index < 0:
            raise KeyError("/".join(parts))
        values = list(entries)
        current = values[index]
        if len(parts) == 1:
            values[index] = callback(current)
        else:
            children = tuple(_coerce_entry(child) for child in current.children)
            values[index] = replace(
                current,
                children=cls._transform(children, parts[1:], callback),
            )
        return tuple(values)


class JsonCodec:
    def loads(self, text: str) -> Sequence[Entry | Mapping[str, Any]]:
        value = json.loads(text)
        if not isinstance(value, list):
            raise TypeError("Cordis configuration must be a list of entries")
        return value

    def dumps(self, entries: Sequence[Entry]) -> str:
        return (
            json.dumps(
                [_entry_data(entry) for entry in entries],
                ensure_ascii=False,
                indent=2,
            )
            + "\n"
        )


class YamlCodec:
    @staticmethod
    def _yaml() -> Any:
        try:
            return importlib.import_module("yaml")
        except ImportError as error:
            raise ConfigError(
                "YAML configuration requires the optional 'yaml' extra"
            ) from error

    def loads(self, text: str) -> Sequence[Entry | Mapping[str, Any]]:
        value = self._yaml().safe_load(text)
        if not isinstance(value, list):
            raise TypeError("Cordis configuration must be a list of entries")
        return value

    def dumps(self, entries: Sequence[Entry]) -> str:
        return str(
            self._yaml().safe_dump(
                [_entry_data(entry) for entry in entries],
                allow_unicode=True,
                sort_keys=False,
            )
        )


class FileBackend:
    """UTF-8 file storage with atomic replacement."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    @property
    def identity(self) -> str:
        return str(self.path)

    async def read(self) -> str | None:
        try:
            return await asyncio.to_thread(self.path.read_text, encoding="utf-8")
        except FileNotFoundError:
            return None

    async def write(self, text: str) -> None:
        await asyncio.to_thread(self._write, text)

    def _write(self, text: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(
            prefix=f".{self.path.name}.", dir=self.path.parent, text=True
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                stream.write(text)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        except BaseException:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
            raise


def codec_for_path(path: str | Path) -> ConfigCodec:
    suffix = Path(path).suffix.lower()
    if suffix == ".json":
        return JsonCodec()
    if suffix in {".yaml", ".yml"}:
        return YamlCodec()
    raise ValueError(f"writable configuration format is not supported: {suffix}")


def _fingerprint(text: str | None) -> str | None:
    return None if text is None else hashlib.sha256(text.encode()).hexdigest()


async def _serialized(lock: asyncio.Lock, operation: Callable[[], Awaitable[T]]) -> T:
    async def locked() -> T:
        async with lock:
            return await operation()

    task = asyncio.create_task(locked(), name="cordis:config-store")
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError as cancelled:
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
            except BaseException:
                break
        try:
            task.result()
        except BaseException as error:
            raise cancelled from error
        raise


class ConfigStore:
    """Transactionally keep an EntryTree, a Loader, and storage in sync."""

    def __init__(
        self,
        loader: Loader[Any],
        backend: ConfigBackend,
        codec: ConfigCodec,
    ) -> None:
        self.loader = loader
        self.backend = backend
        self.codec = codec
        self._tree = EntryTree(tuple(loader.entries))
        self._revision: str | None = None
        self._lock = asyncio.Lock()

    @classmethod
    def file(
        cls,
        loader: Loader[Any],
        path: str | Path,
        *,
        codec: ConfigCodec | None = None,
    ) -> ConfigStore:
        return cls(loader, FileBackend(path), codec or codec_for_path(path))

    @property
    def tree(self) -> EntryTree:
        return self._tree

    async def _run(self, operation: Callable[[], Awaitable[T]]) -> T:
        return await _serialized(self._lock, operation)

    async def _load(self) -> EntryTree:
        text = await self.backend.read()
        if text is None:
            raise FileNotFoundError(self.backend.identity)
        tree = EntryTree.from_values(self.codec.loads(text))
        await self.loader.reconcile(tree.entries)
        self._tree = tree
        self._revision = _fingerprint(text)
        return tree

    async def load(self) -> EntryTree:
        return await self._run(self._load)

    async def _save_current(self) -> None:
        await self._save(self._tree)

    async def save(self) -> None:
        await self._run(self._save_current)

    async def _replace(self, tree: EntryTree, persist: bool) -> EntryTree:
        previous = self._tree
        try:
            await self.loader.reconcile(tree.entries)
            if persist:
                await self._save(tree)
        except BaseException as error:
            try:
                await self.loader.reconcile(previous.entries)
            except BaseException as rollback_error:
                raise BaseExceptionGroup(
                    "configuration update and rollback both failed",
                    [error, rollback_error],
                ) from error
            raise
        self._tree = tree
        return tree

    async def replace(self, tree: EntryTree, *, persist: bool = True) -> EntryTree:
        return await self._run(lambda: self._replace(tree, persist))

    async def _save(self, tree: EntryTree) -> None:
        current = await self.backend.read()
        if _fingerprint(current) != self._revision:
            raise StaleConfigError(
                f"configuration changed outside this store: {self.backend.identity}"
            )
        text = self.codec.dumps(tree.entries)
        await self.backend.write(text)
        self._revision = _fingerprint(text)

    async def create(
        self,
        entry: Entry | Mapping[str, Any],
        *,
        parent: EntryPath | None = None,
        position: int | None = None,
    ) -> EntryTree:
        async def create() -> EntryTree:
            tree = self._tree.create(entry, parent=parent, position=position)
            return await self._replace(tree, True)

        return await self._run(create)

    async def update(
        self,
        path: EntryPath,
        entry: Entry | Mapping[str, Any] | None = None,
        **changes: object,
    ) -> EntryTree:
        async def update() -> EntryTree:
            tree = self._tree.update(path, entry, **changes)
            return await self._replace(tree, True)

        return await self._run(update)

    async def remove(self, path: EntryPath) -> Entry:
        async def remove() -> Entry:
            tree, removed = self._tree.remove(path)
            await self._replace(tree, True)
            return removed

        return await self._run(remove)

    async def move(
        self,
        path: EntryPath,
        *,
        parent: EntryPath | None = None,
        position: int | None = None,
    ) -> EntryTree:
        async def move() -> EntryTree:
            tree = self._tree.move(path, parent=parent, position=position)
            return await self._replace(tree, True)

        return await self._run(move)

    async def watch(
        self,
        *,
        interval: float = 0.5,
        stop: asyncio.Event | None = None,
    ) -> None:
        while stop is None or not stop.is_set():
            await asyncio.sleep(interval)
            text = await self.backend.read()
            if _fingerprint(text) != self._revision:
                await self.load()


class ConfigIncludes:
    """Mount independently persisted configuration files as Loader groups."""

    def __init__(self, loader: Loader[Any]) -> None:
        self.loader = loader
        self._stores: dict[str, ConfigStore] = {}
        self._lock = asyncio.Lock()

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(self._stores)

    def __getitem__(self, name: str) -> ConfigStore:
        return self._stores[name]

    async def include(
        self,
        name: str,
        backend: ConfigBackend,
        codec: ConfigCodec,
    ) -> ConfigStore:
        if not name:
            raise ValueError("include name must be non-empty")

        async def include() -> ConfigStore:
            if name in self._stores or any(
                entry.id == name for entry in self.loader.entries
            ):
                raise ValueError(f"include {name!r} already exists")
            previous = self.loader.entries
            await self.loader.reconcile((*previous, Entry(name)))
            child = self.loader.child(name)
            if child is None:
                await self.loader.reconcile(previous)
                raise ConfigError(f"include group {name!r} did not create a Loader")
            store = ConfigStore(child, backend, codec)
            try:
                await store.load()
            except BaseException as error:
                try:
                    await self.loader.reconcile(previous)
                except BaseException as rollback_error:
                    raise BaseExceptionGroup(
                        "configuration include and rollback both failed",
                        [error, rollback_error],
                    ) from error
                raise
            self._stores[name] = store
            return store

        return await _serialized(self._lock, include)

    async def include_file(
        self,
        name: str,
        path: str | Path,
        *,
        codec: ConfigCodec | None = None,
    ) -> ConfigStore:
        return await self.include(
            name,
            FileBackend(path),
            codec or codec_for_path(path),
        )

    async def remove(self, name: str) -> ConfigStore:
        async def remove() -> ConfigStore:
            store = self._stores[name]
            entries = tuple(entry for entry in self.loader.entries if entry.id != name)
            await self.loader.reconcile(entries)
            del self._stores[name]
            return store

        return await _serialized(self._lock, remove)
