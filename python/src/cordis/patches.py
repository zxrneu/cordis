"""Immutable configuration overlays and three-way reconciliation."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, fields, replace
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

from .loader import Entry

if TYPE_CHECKING:
    from .config import EntryPath, EntryTree


def _path(value: EntryPath | None) -> tuple[str, ...]:
    if value is None:
        return ()
    return (
        tuple(part for part in value.split("/") if part)
        if isinstance(value, str)
        else tuple(value)
    )


@dataclass(frozen=True, slots=True)
class ConfigPatch:
    """Override fields at path, or insert entries under a group (None=root)."""

    path: EntryPath | None = None
    changes: Mapping[str, object] = field(default_factory=dict)
    insert: tuple[Entry, ...] = ()

    def __post_init__(self) -> None:
        path = _path(self.path)
        if any(not isinstance(part, str) or not part for part in path):
            raise ValueError("patch paths must contain non-empty string ids")
        object.__setattr__(self, "path", path)
        object.__setattr__(self, "changes", MappingProxyType(dict(self.changes)))
        object.__setattr__(self, "insert", tuple(self.insert))
        if self.insert and self.changes:
            raise ValueError("a patch must either insert entries or override fields")
        if "id" in self.changes or "children" in self.changes:
            raise ValueError(
                "patch ids are stable; insert children with an insertion patch"
            )
        valid = {item.name for item in fields(Entry)}
        if set(self.changes) - valid:
            raise ValueError(
                "unknown patch fields: " + ", ".join(sorted(set(self.changes) - valid))
            )


@dataclass(frozen=True, slots=True)
class ConfigConflict:
    path: tuple[str, ...]
    field: str
    message: str


@dataclass(frozen=True, slots=True)
class ConfigChange:
    path: tuple[str, ...]
    before: Entry | None
    after: Entry | None


def _entries(values: Sequence[Entry | Mapping[str, Any]]) -> tuple[Entry, ...]:
    return tuple(
        value if isinstance(value, Entry) else Entry(**value) for value in values
    )


def _index(tree: EntryTree) -> dict[tuple[str, ...], Entry]:
    result: dict[tuple[str, ...], Entry] = {}

    def visit(entries: tuple[Entry, ...], parent: tuple[str, ...]) -> None:
        for entry in entries:
            path = (*parent, entry.id)
            result[path] = entry
            visit(_entries(entry.children), path)

    visit(tree.entries, ())
    return result


def changes(before: EntryTree, after: EntryTree) -> tuple[ConfigChange, ...]:
    old, new = _index(before), _index(after)
    return tuple(
        ConfigChange(path, old.get(path), new.get(path))
        for path in sorted(old.keys() | new.keys())
        if old.get(path) != new.get(path)
    )


def apply_patches(
    tree: EntryTree, patches: Sequence[ConfigPatch], *, strict: bool = True
) -> EntryTree:
    for patch in patches:
        if patch.path:
            try:
                tree.get(patch.path)
            except KeyError:
                if strict:
                    raise
                continue
        if patch.insert:
            for entry in patch.insert:
                tree = tree.create(entry, parent=patch.path or None)
        elif patch.changes:
            if not patch.path:
                raise ValueError("an override patch requires a path")
            tree = tree.update(patch.path, entry=None, **patch.changes)
    return tree


def route_patches(
    raw: EntryTree,
    desired: EntryTree,
    patches: Sequence[ConfigPatch],
) -> tuple[EntryTree, tuple[ConfigPatch, ...]]:
    """Return file-owned entries and updated overlays without flattening ownership."""
    indexed = _index(desired)
    raw_index = _index(raw)
    result: list[ConfigPatch] = []
    file_tree = desired

    def locate(path: tuple[str, ...], entry_id: str) -> tuple[str, ...] | None:
        if path in indexed:
            return path
        matches = [candidate for candidate in indexed if candidate[-1] == entry_id]
        if len(matches) > 1:
            raise ValueError(f"ambiguous moved patch entry {entry_id!r}")
        return matches[0] if matches else None

    # Inserts own their complete subtrees, including children added at runtime.
    inserted: list[tuple[str, ...]] = []
    for patch in patches:
        if not patch.insert:
            result.append(patch)
            continue
        parent = _path(patch.path)
        groups: dict[tuple[str, ...], list[Entry]] = {}
        for entry in patch.insert:
            current = locate((*parent, entry.id), entry.id)
            if current is None:
                continue
            groups.setdefault(current[:-1], []).append(indexed[current])
            inserted.append(current)
        result.extend(
            ConfigPatch(parent or None, insert=tuple(entries))
            for parent, entries in groups.items()
        )
    for path in sorted(inserted, key=len, reverse=True):
        try:
            file_tree, _ = file_tree.remove(path)
        except KeyError:
            pass

    # Last overlay wins each field. Keep the unpatched value in the file.
    owners: dict[tuple[tuple[str, ...], str], int] = {}
    removed: set[int] = set()
    for index, patch in enumerate(result):
        if not patch.insert:
            for name in patch.changes:
                owners[(_path(patch.path), name)] = index
    for index, patch in enumerate(result):
        if patch.insert or not patch.path:
            continue
        original = _path(patch.path)
        current = locate(original, original[-1])
        if current is None:
            removed.add(index)
            continue
        values = dict(patch.changes)
        restore: dict[str, object] = {}
        for name in values:
            if owners.get((original, name)) == index:
                values[name] = getattr(indexed[current], name)
                source = raw_index.get(original) or raw_index.get(current)
                if source is not None:
                    restore[name] = getattr(source, name)
        result[index] = ConfigPatch(current, values)
        if restore and not any(current[: len(path)] == path for path in inserted):
            file_tree = file_tree.update(current, entry=None, **restore)
    return file_tree, tuple(
        patch for index, patch in enumerate(result) if index not in removed
    )


def merge_trees(
    base: EntryTree, ours: EntryTree, theirs: EntryTree
) -> tuple[EntryTree, tuple[ConfigConflict, ...]]:
    """Merge disjoint edits; the file wins overlapping edits and removals."""
    from .config import EntryTree

    conflicts: list[ConfigConflict] = []

    def conflict(path: tuple[str, ...], name: str, message: str) -> None:
        conflicts.append(ConfigConflict(path, name, message))

    def merge(
        b: tuple[Entry, ...],
        o: tuple[Entry, ...],
        t: tuple[Entry, ...],
        parent: tuple[str, ...],
    ) -> tuple[Entry, ...]:
        bm, om, tm = ({entry.id: entry for entry in values} for values in (b, o, t))
        order = list(tm)
        common = bm.keys() & om.keys() & tm.keys()
        bo, oo, to = (
            [entry.id for entry in values if entry.id in common] for values in (b, o, t)
        )
        if oo != bo and to == bo:
            order = [entry.id for entry in o if entry.id in tm or entry.id not in bm]
            order.extend(key for key in tm if key not in order)
        elif oo != bo and to != bo and oo != to:
            conflict(parent, "order", "entry order changed both in file and at runtime")
        order.extend(key for key in om if key not in tm and key not in bm)
        merged = []
        for key in order:
            old, own, other = bm.get(key), om.get(key), tm.get(key)
            path = (*parent, key)
            if old is None:
                if own is not None and other is not None and own != other:
                    conflict(path, "entry", "entry created both in file and at runtime")
                value = other if other is not None else own
                if value is not None:
                    merged.append(value)
                continue
            if own is None:
                if other is not None and other != old:
                    conflict(path, "entry", "modified in file, removed at runtime")
                    merged.append(other)
                continue
            if other is None:
                if own != old:
                    conflict(path, "entry", "removed in file, modified at runtime")
                continue
            updates: dict[str, Any] = {}
            for descriptor in fields(Entry):
                name = descriptor.name
                if name in ("id", "children"):
                    continue
                bv, ov, tv = (
                    getattr(old, name),
                    getattr(own, name),
                    getattr(other, name),
                )
                if ov != bv and tv == bv:
                    updates[name] = ov
                elif ov != bv and tv != ov:
                    conflict(path, name, "field changed both in file and at runtime")
            updates["children"] = merge(
                _entries(old.children),
                _entries(own.children),
                _entries(other.children),
                path,
            )
            merged.append(replace(other, **updates))
        # File deletions are authoritative, including entries omitted from order.
        for key in bm.keys() & om.keys() - tm.keys():
            if om[key] != bm[key]:
                conflict(
                    (*parent, key), "entry", "removed in file, modified at runtime"
                )
        return tuple(merged)

    return EntryTree(merge(base.entries, ours.entries, theirs.entries, ())), tuple(
        conflicts
    )
