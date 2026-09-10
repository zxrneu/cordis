from __future__ import annotations

import asyncio
from pathlib import Path

from cordis import Entry, FiberState, Loader

from .config import AppConfig, format_cents, parse_config
from .contracts import AppContext
from .plugins import console, database, importer, validation


class Application:
    """Convert validated business settings to a stable Cordis plugin tree."""

    def __init__(self, ctx: AppContext, path: Path) -> None:
        self.ctx = ctx
        self.path = path.resolve()
        self.loader = Loader(ctx)
        self.config: AppConfig | None = None
        self._observed: bytes | None = None

    async def refresh(self) -> bool:
        content = await asyncio.to_thread(self.path.read_bytes)
        if content == self._observed:
            return False
        self._observed = content
        # Invalid syntax or fields never reach Loader or change live resources.
        config = parse_config(content, self.path.parent)
        if config == self.config:
            return False
        await self.loader.reconcile(
            [
                Entry("console", console, enabled=config.logging),
                Entry("database", database, config.database),
                Entry("validation", validation, config.validation),
                Entry("importer", importer, config.importer),
            ]
        )
        for fiber in self.ctx.fibers:
            await fiber
            if fiber.state is not FiberState.ACTIVE:
                raise RuntimeError(f"{fiber.name} 未激活: {fiber.state.value}")
        self.config = config
        print(
            f"[配置已更新] 金额上限 {format_cents(config.validation.max_amount_cents)}，"
            f"扫描间隔 {config.importer.interval:g}s，日志 {config.logging}",
            flush=True,
        )
        self.ctx.emit("config/applied", config)
        return True

    async def watch(self, interval: float = 0.2) -> None:
        last_error: str | None = None
        while True:
            try:
                await self.refresh()
                last_error = None
            except Exception as error:
                message = str(error)
                if message != last_error:
                    print(f"[配置错误] {message}；等待下一次保存", flush=True)
                    self.ctx.emit("config/error", error)
                    last_error = message
            await asyncio.sleep(interval)
