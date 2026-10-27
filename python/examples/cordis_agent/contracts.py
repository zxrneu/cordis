from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol


Json = dict[str, Any]
ToolHandler = Callable[[Mapping[str, Any]], Awaitable[Any] | Any]


class LanguageModel(Protocol):
    """Return an assistant message for the supplied conversation and tools."""

    async def complete(
        self, messages: Sequence[Json], tools: Sequence[Json]
    ) -> Json: ...


@dataclass(frozen=True, slots=True)
class Tool:
    name: str
    description: str
    parameters: Json
    execute: ToolHandler
    mutating: bool = False


@dataclass(frozen=True, slots=True)
class ToolCall:
    id: str
    name: str
    arguments: Json


def required_string(
    arguments: Mapping[str, Any], key: str, *, allow_empty: bool = False
) -> str:
    value = arguments.get(key)
    if not isinstance(value, str) or (not allow_empty and not value):
        raise ValueError(f"{key} 必须是{'可为空的' if allow_empty else '非空'}字符串")
    return value


def object_schema(properties: Json, required: list[str]) -> Json:
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }
