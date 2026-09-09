from __future__ import annotations

import ast
import asyncio
import operator
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Any

from cordis import Context

from .contracts import Json, Tool, ToolCall, required_string


class ApprovalPolicy:
    def __init__(self, *, interactive: bool):
        self.interactive = interactive

    async def authorize(self, tool: Tool, call: ToolCall) -> str | None:
        if not tool.mutating:
            return None
        if not self.interactive:
            return f"非交互模式拒绝修改型工具 {tool.name!r}"
        answer = await asyncio.to_thread(
            input,
            f"\n模型请求执行修改型工具 {tool.name}，参数 {call.arguments!r}\n允许？[y/N] ",
        )
        return None if answer.strip().lower() in {"y", "yes"} else "用户拒绝执行"


class ToolRuntime:
    def __init__(self, ctx: Context):
        self.ctx = ctx
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> Callable[[], None]:
        if tool.name in self._tools:
            raise ValueError(f"工具重复注册：{tool.name}")
        self._tools[tool.name] = tool

        def unregister() -> None:
            if self._tools.get(tool.name) is tool:
                del self._tools[tool.name]

        return unregister

    def schemas(self) -> list[Json]:
        return [
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.parameters,
                },
            }
            for tool in self._tools.values()
        ]

    async def execute(self, call: ToolCall) -> Json:
        tool = self._tools.get(call.name)
        if tool is None:
            return {"ok": False, "error": f"未知工具 {call.name!r}"}
        if denial := await self.ctx.policy.authorize(tool, call):
            return {"ok": False, "error": denial}
        try:
            value = tool.execute(call.arguments)
            if isinstance(value, Awaitable):
                value = await value
            return {"ok": True, "value": value}
        except Exception as error:
            return {"ok": False, "error": f"{type(error).__name__}: {error}"}


class Workspace:
    def __init__(self, root: Path):
        self.root = root

    @classmethod
    async def open(cls, root: Path) -> Workspace:
        return cls(await asyncio.to_thread(root.resolve))

    async def resolve(self, raw: Any) -> Path:
        return await asyncio.to_thread(self._resolve, raw)

    def _resolve(self, raw: Any) -> Path:
        path = (self.root / str(raw or ".")).resolve()
        if path != self.root and self.root not in path.parents:
            raise PermissionError("路径超出 workspace")
        if path == self.root / ".git" or self.root / ".git" in path.parents:
            raise PermissionError("禁止访问 .git")
        return path

    async def list_files(self, arguments: Mapping[str, Any]) -> list[str]:
        return await asyncio.to_thread(self._list_files, arguments)

    def _list_files(self, arguments: Mapping[str, Any]) -> list[str]:
        directory = self._resolve(arguments.get("path", "."))
        if not directory.is_dir():
            raise NotADirectoryError(directory)
        result: list[str] = []
        for path in sorted(directory.rglob("*")):
            if ".git" in path.parts or not path.is_file():
                continue
            result.append(str(path.relative_to(self.root)))
            if len(result) == 200:
                break
        return result

    async def read_file(self, arguments: Mapping[str, Any]) -> str:
        return await asyncio.to_thread(self._read_file, arguments)

    def _read_file(self, arguments: Mapping[str, Any]) -> str:
        path = self._resolve(required_string(arguments, "path"))
        if path.stat().st_size > 100_000:
            raise ValueError("文件超过 100 KB")
        return path.read_text(encoding="utf-8")

    async def write_file(self, arguments: Mapping[str, Any]) -> str:
        return await asyncio.to_thread(self._write_file, arguments)

    def _write_file(self, arguments: Mapping[str, Any]) -> str:
        path = self._resolve(required_string(arguments, "path"))
        content = required_string(arguments, "content", allow_empty=True)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return f"已写入 {path.relative_to(self.root)} ({len(content)} 字符)"


_OPERATORS: dict[type[ast.operator], Callable[[float, float], float]] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
}


def calculate(arguments: Mapping[str, Any]) -> int | float:
    def visit(node: ast.AST) -> int | float:
        if isinstance(node, ast.Constant) and type(node.value) in (int, float):
            return node.value
        if isinstance(node, ast.BinOp) and type(node.op) in _OPERATORS:
            return _OPERATORS[type(node.op)](visit(node.left), visit(node.right))
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
            return -visit(node.operand)
        raise ValueError("只允许数字、括号和 + - * /")

    expression = required_string(arguments, "expression")
    return visit(ast.parse(expression, mode="eval").body)
