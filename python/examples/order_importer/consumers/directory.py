from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path

from cordis import plugin

from ..definitions import (
    EVENT_ORDERS_IMPORT_FAILED,
    EVENT_ORDERS_IMPORTED,
    IMPORTER,
    AppContext,
    ImportRequest,
)


@dataclass(frozen=True)
class ImporterConfig:
    directory: Path
    interval: float


class DirectoryWatcher:
    def __init__(self, ctx: AppContext, config: ImporterConfig) -> None:
        self.ctx = ctx
        self.config = config
        self.seen: dict[Path, tuple[int, int]] = {}

    async def scan_step(self) -> None:
        for path in sorted(self.config.directory.glob("*.csv")):
            try:
                stat = path.stat()
            except FileNotFoundError:
                continue
            signature = (stat.st_mtime_ns, stat.st_size)
            if self.seen.get(path) == signature:
                continue
            self.seen[path] = signature
            try:
                # Cancelling the scanner must not cancel a draining import.
                report = await asyncio.shield(
                    self.ctx.importer.submit(ImportRequest(path))
                )
            except Exception as error:
                self.ctx.emit(EVENT_ORDERS_IMPORT_FAILED, path, error)
            else:
                self.ctx.emit(EVENT_ORDERS_IMPORTED, report)


@plugin(inject=[IMPORTER])
def directory(ctx: AppContext, config: ImporterConfig) -> None:
    config.directory.mkdir(parents=True, exist_ok=True)
    watcher = DirectoryWatcher(ctx, config)
    ctx.timer.interval(
        config.interval, watcher.scan_step, immediate=True, policy="cancel"
    )
