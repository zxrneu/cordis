from __future__ import annotations

import asyncio
import math
from collections.abc import AsyncIterator, Iterable
from pathlib import Path


async def watch_paths(
    paths: Iterable[str | Path],
    *,
    interval: float,
    stop: asyncio.Event | None = None,
    ready: asyncio.Event | None = None,
) -> AsyncIterator[tuple[Path, ...]]:
    """Poll files or Python source directories, including additions and deletions."""
    if not math.isfinite(interval) or interval <= 0:
        raise ValueError("watch interval must be finite and positive")
    roots = tuple(Path(path).resolve() for path in paths)

    def snapshot() -> dict[Path, tuple[int, int] | None]:
        result: dict[Path, tuple[int, int] | None] = {}
        for root in roots:
            files = root.rglob("*.py") if root.is_dir() else (root,)
            for path in files:
                try:
                    stat = path.stat()
                    result[path] = stat.st_mtime_ns, stat.st_size
                except FileNotFoundError:
                    result[path] = None
        return result

    previous = await asyncio.to_thread(snapshot)
    if ready is not None:
        ready.set()
    while stop is None or not stop.is_set():
        if stop is None:
            await asyncio.sleep(interval)
        else:
            try:
                await asyncio.wait_for(stop.wait(), interval)
                return
            except TimeoutError:
                pass
        current = await asyncio.to_thread(snapshot)
        changed = tuple(
            sorted(
                path
                for path in previous.keys() | current.keys()
                if previous.get(path) != current.get(path)
            )
        )
        previous = current
        if changed:
            yield changed
