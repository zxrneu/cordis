from __future__ import annotations

import asyncio
import json
from pathlib import Path

from .contracts import Json


class SessionLog:
    """保存 API 原生 message；磁盘操作通过 asyncio 委托，避免阻塞 loop。"""

    def __init__(self, path: Path):
        self.path = path
        self.messages: list[Json] = []
        self._lock = asyncio.Lock()

    @classmethod
    async def open(cls, path: Path, *, resume: bool) -> SessionLog:
        session = cls(path)
        await asyncio.to_thread(session._initialize, resume)
        return session

    def _initialize(self, resume: bool) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if resume and self.path.exists():
            for line in self.path.read_text(encoding="utf-8").splitlines():
                record = json.loads(line)
                if record.get("type") == "message":
                    self.messages.append(record["message"])
        else:
            self.path.write_text("", encoding="utf-8")

    async def append(self, message: Json) -> None:
        async with self._lock:
            await asyncio.to_thread(self._append, message)

    def _append(self, message: Json) -> None:
        record = {"type": "message", "message": message}
        with self.path.open("a", encoding="utf-8") as file:
            file.write(json.dumps(record, ensure_ascii=False) + "\n")
        self.messages.append(message)
