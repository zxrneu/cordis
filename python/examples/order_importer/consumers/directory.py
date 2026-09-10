from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path

from cordis import plugin

from ..definitions import IMPORTER, AppContext, ImportRequest


@dataclass(frozen=True)
class ImporterConfig:
    directory: Path
    interval: float


class DirectoryWatcher:
    def __init__(self, ctx: AppContext, config: ImporterConfig) -> None:
        self.ctx = ctx
        self.config = config

    async def watch(self) -> None:
        seen: dict[Path, tuple[int, int]] = {}
        while True:
            for path in sorted(self.config.directory.glob("*.csv")):
                try:
                    stat = path.stat()
                except FileNotFoundError:
                    continue
                signature = (stat.st_mtime_ns, stat.st_size)
                if seen.get(path) == signature:
                    continue
                seen[path] = signature
                try:
                    # Cancelling the scanner must not cancel a draining import.
                    report = await asyncio.shield(
                        self.ctx.importer.submit(ImportRequest(path))
                    )
                except Exception as error:
                    self.ctx.emit("orders/import-failed", path, error)
                else:
                    self.ctx.emit("orders/imported", report)
            await asyncio.sleep(self.config.interval)


@plugin(inject=[IMPORTER])
def directory(ctx: AppContext, config: ImporterConfig) -> None:
    config.directory.mkdir(parents=True, exist_ok=True)
    watcher = DirectoryWatcher(ctx, config)
    ctx.spawn(watcher.watch(), policy="cancel", name="orders:scan")
