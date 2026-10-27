from __future__ import annotations

import asyncio
import json
from typing import Any

from .contracts import LanguageModel, ToolCall
from .session import SessionLog
from .tools import ToolRuntime


class AgentRuntime:
    def __init__(
        self,
        session: SessionLog,
        model: LanguageModel,
        tools: ToolRuntime,
        system_prompt: str,
        *,
        max_steps: int,
    ) -> None:
        self.session = session
        self.model = model
        self.tools = tools
        self.system_prompt = system_prompt
        self.max_steps = max_steps
        self._lock = asyncio.Lock()

    async def run(self, text: str) -> str:
        async with self._lock:
            return await self._run(text)

    async def _run(self, text: str) -> str:
        session = self.session
        if not session.messages:
            await session.append({"role": "system", "content": self.system_prompt})
        await session.append({"role": "user", "content": text})

        for _ in range(self.max_steps):
            message = await self.model.complete(
                session.messages,
                self.tools.schemas(),
            )
            await session.append(message)
            calls = message.get("tool_calls") or []
            if not calls:
                return message.get("content") or ""

            for raw in calls:
                call = self._parse_call(raw)
                result = await self.tools.execute(call)
                await session.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.id,
                        "content": json.dumps(result, ensure_ascii=False),
                    }
                )
        raise RuntimeError(f"Agent 超过最大工具迭代次数 {self.max_steps}")

    @staticmethod
    def _parse_call(raw: Any) -> ToolCall:
        try:
            call_id = raw["id"]
            function = raw["function"]
            name = function["name"]
            arguments = json.loads(function["arguments"])
        except (KeyError, TypeError, json.JSONDecodeError) as error:
            raise ValueError(f"模型返回了无效 ToolCall：{raw!r}") from error
        if not all(isinstance(value, str) and value for value in (call_id, name)):
            raise ValueError(f"ToolCall id/name 无效：{raw!r}")
        if not isinstance(arguments, dict):
            raise ValueError("ToolCall arguments 必须是 JSON object")
        return ToolCall(call_id, name, arguments)
