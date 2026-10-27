"""一个离线、可运行的 DSH-like Agent Harness。

它不是 DeepSeek Harness 的 Python 移植，而是用本仓库 Cordis 展示同一种
"everything is a plugin" 组装方式：模型、工具、策略、会话、记忆、Prompt、
Agent Loop 和 headless app 都是可替换插件。
"""

from __future__ import annotations

import ast
import asyncio
import json
import operator
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

from cordis import Context, Entry, Loader, ServiceKey, plugin


JsonObject = dict[str, Any]
ToolHandler = Callable[[Mapping[str, Any]], Awaitable[Any] | Any]


@dataclass(frozen=True, slots=True)
class Tool:
    name: str
    description: str
    execute: ToolHandler


@dataclass(frozen=True, slots=True)
class ToolCall:
    id: str
    name: str
    arguments: JsonObject


@dataclass(frozen=True, slots=True)
class ModelReply:
    content: str | None = None
    tool_call: ToolCall | None = None


async def maybe_await(value: Any) -> Any:
    return await value if isinstance(value, Awaitable) else value


class SessionLog:
    """只追加、不原地修改的运行轨迹。"""

    def __init__(self) -> None:
        self.events: list[JsonObject] = []

    def append(self, kind: str, **payload: Any) -> JsonObject:
        event = {"seq": len(self.events) + 1, "kind": kind, **payload}
        self.events.append(event)
        return event

    def latest(self) -> JsonObject:
        return self.events[-1]


class Policy:
    def __init__(self, allowed: list[str]):
        self.allowed = frozenset(allowed)

    def authorize(self, call: ToolCall) -> str | None:
        if call.name in self.allowed:
            return None
        return f"策略拒绝工具 {call.name!r}"


class ToolRuntime:
    """工具定义注册表 + 策略化执行入口。"""

    def __init__(self, policy: Policy, owner: Context) -> None:
        self.policy = policy
        self.owner = owner
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> Callable[[], None]:
        if tool.name in self._tools:
            raise ValueError(f"工具重复注册：{tool.name}")
        self._tools[tool.name] = tool

        def unregister() -> None:
            if self._tools.get(tool.name) is tool:
                del self._tools[tool.name]

        return unregister

    def schemas(self) -> list[JsonObject]:
        return [
            {"name": tool.name, "description": tool.description}
            for tool in self._tools.values()
        ]

    async def execute(self, call: ToolCall) -> JsonObject:
        denial = self.policy.authorize(call)
        if denial:
            result = {"ok": False, "error": denial}
        elif (tool := self._tools.get(call.name)) is None:
            result = {"ok": False, "error": f"未知工具 {call.name!r}"}
        else:
            try:
                value = await maybe_await(tool.execute(call.arguments))
                result = {"ok": True, "value": value}
            except Exception as error:
                result = {"ok": False, "error": f"{type(error).__name__}: {error}"}
        self.owner.emit("tools/result", call, result)
        return result


class FakeDeepSeek:
    """确定性的模型替身：把几种中文指令翻译成 ToolCall。"""

    def __init__(self, model: str):
        self.model = model

    async def complete(
        self,
        events: list[JsonObject],
        tools: list[JsonObject],
        system_prompt: str,
    ) -> ModelReply:
        await asyncio.sleep(0)
        latest = events[-1]
        if latest["kind"] == "tool_result":
            rendered = json.dumps(latest["result"], ensure_ascii=False)
            return ModelReply(content=f"{self.model} 根据工具结果回答：{rendered}")

        user = latest["content"].strip()
        available = {schema["name"] for schema in tools}
        call_id = f"call-{latest['seq']:04d}"

        if user.startswith("计算 ") and "calculator" in available:
            call = ToolCall(call_id, "calculator", {"expression": user[3:]})
        elif user.startswith("记住 ") and "remember" in available:
            key, separator, value = user[3:].partition("=")
            if not separator:
                return ModelReply(content="格式应为：记住 key=value")
            call = ToolCall(call_id, "remember", {"key": key, "value": value})
        elif user.startswith("回忆 ") and "recall" in available:
            call = ToolCall(call_id, "recall", {"key": user[3:]})
        elif user.startswith("大写 ") and "uppercase" in available:
            call = ToolCall(call_id, "uppercase", {"text": user[3:]})
        else:
            return ModelReply(content=f"{self.model}：{system_prompt}；收到“{user}”")
        return ModelReply(tool_call=call)


class AgentRuntime:
    def __init__(
        self, session: SessionLog, model: FakeDeepSeek, tools: ToolRuntime, prompt: str
    ) -> None:
        self.session = session
        self.model = model
        self.tools = tools
        self.prompt = prompt

    async def run(self, user: str) -> str:
        session = self.session
        session.append("user", content=user)

        for _ in range(4):
            reply = await self.model.complete(
                session.events,
                self.tools.schemas(),
                self.prompt,
            )
            if reply.tool_call is None:
                answer = reply.content or ""
                session.append("assistant", content=answer)
                return answer

            call = reply.tool_call
            session.append(
                "tool_call",
                id=call.id,
                name=call.name,
                arguments=call.arguments,
            )
            result = await self.tools.execute(call)
            session.append("tool_result", id=call.id, name=call.name, result=result)

        raise RuntimeError("Agent Loop 超过最大迭代次数")


SESSION = ServiceKey[SessionLog]("session")
POLICY = ServiceKey[Policy]("policy")
TOOLS = ServiceKey[ToolRuntime]("tools")
MEMORY = ServiceKey[dict[str, object]]("memory")
MODEL = ServiceKey[FakeDeepSeek]("llm")
PROMPT = ServiceKey[str]("system_prompt")
AGENT = ServiceKey[AgentRuntime]("agent")


@plugin(provide=[SESSION])
def session_plugin(ctx: Context, _config: None) -> None:
    ctx.provide(SESSION, SessionLog())


@plugin(provide=[MEMORY])
def memory_plugin(ctx: Context, _config: None) -> None:
    ctx.provide(MEMORY, {})


@plugin(provide=[POLICY])
def policy_plugin(ctx: Context, config: JsonObject) -> None:
    ctx.provide(POLICY, Policy(config["allow"]))


@plugin(inject=[POLICY], provide=[TOOLS])
def tools_plugin(ctx: Context, _config: None) -> None:
    ctx.provide(TOOLS, ToolRuntime(ctx.service(POLICY), ctx))


@plugin(inject=[TOOLS])
def calculator_tool(ctx: Context, _config: None) -> None:
    ctx.defer(
        ctx.service(TOOLS).register(Tool("calculator", "计算四则表达式", calculate))
    )


@plugin(inject=[TOOLS, MEMORY])
def memory_tools(ctx: Context, _config: None) -> None:
    memory = ctx.service(MEMORY)

    def remember(arguments: Mapping[str, Any]) -> str:
        key = str(arguments["key"])
        memory[key] = arguments["value"]
        return f"已记住 {key}"

    def recall(arguments: Mapping[str, Any]) -> Any:
        return memory.get(str(arguments["key"]), "未找到")

    for tool in (
        Tool("remember", "保存一条长期记忆", remember),
        Tool("recall", "读取一条长期记忆", recall),
    ):
        ctx.defer(ctx.service(TOOLS).register(tool))


@plugin(inject=[TOOLS])
def uppercase_tool(ctx: Context, _config: None) -> None:
    def uppercase(arguments: Mapping[str, Any]) -> str:
        return str(arguments["text"]).upper()

    ctx.defer(
        ctx.service(TOOLS).register(Tool("uppercase", "把文本转换为大写", uppercase))
    )


@plugin(provide=[PROMPT])
def system_prompt_plugin(ctx: Context, config: JsonObject) -> None:
    ctx.provide(PROMPT, config["text"])


@plugin(provide=[MODEL])
def llm_plugin(ctx: Context, config: JsonObject) -> None:
    ctx.provide(MODEL, FakeDeepSeek(config["model"]))


@plugin(
    inject=[MODEL, TOOLS, SESSION, PROMPT],
    provide=[AGENT],
)
def agent_plugin(ctx: Context, _config: None) -> None:
    ctx.provide(
        AGENT,
        AgentRuntime(
            ctx.service(SESSION),
            ctx.service(MODEL),
            ctx.service(TOOLS),
            ctx.service(PROMPT),
        ),
    )


@plugin(inject=[AGENT])
async def headless_plugin(ctx: Context, config: JsonObject) -> None:
    for prompt in config["prompts"]:
        answer = await ctx.service(AGENT).run(prompt)
        print(f"用户> {prompt}\n助手> {answer}\n")


_OPERATORS: dict[type[ast.operator], Callable[[float, float], float]] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
}


def calculate(arguments: Mapping[str, Any]) -> int | float:
    """只解释数字和四则运算，不使用 eval。"""

    def visit(node: ast.AST) -> int | float:
        if isinstance(node, ast.Constant):
            value = node.value
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                return value
        if isinstance(node, ast.BinOp) and type(node.op) in _OPERATORS:
            return _OPERATORS[type(node.op)](visit(node.left), visit(node.right))
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
            return -visit(node.operand)
        raise ValueError("只允许数字、括号和 + - * /")

    tree = ast.parse(str(arguments["expression"]), mode="eval")
    return visit(tree.body)


def base_bundle(allowed_tools: list[str]) -> list[Entry]:
    """类似 dsh-base：组合 Harness 的共享能力。"""

    return [
        Entry("session", session_plugin),
        Entry("memory", memory_plugin),
        Entry("policy", policy_plugin, {"allow": allowed_tools}),
        Entry("tools", tools_plugin),
        Entry("tool/calculator", calculator_tool),
        Entry("tool/memory", memory_tools),
        Entry("tool/uppercase", uppercase_tool),
        Entry("prompt", system_prompt_plugin, {"text": "你是一个插件化助手"}),
        Entry("llm", llm_plugin, {"model": "fake-deepseek-v1"}),
        Entry("agent", agent_plugin),
    ]


def headless_profile(prompts: list[str]) -> Entry:
    """类似 dsh-headless：在 base bundle 上增加一次性应用。"""

    return Entry("app/headless", headless_plugin, {"prompts": prompts})


async def main() -> None:
    async with Context() as root:
        loader = Loader(root)

        # 第一次启动：uppercase 已注册、模型也能看到，但策略拒绝执行。
        base = base_bundle(["calculator", "remember", "recall"])
        await loader.reconcile(
            [
                *base,
                headless_profile(
                    [
                        "计算 (7 + 5) * 3",
                        "记住 project=cordis",
                        "回忆 project",
                        "大写 cordis",
                    ]
                ),
            ]
        )

        # 卸下 one-shot app，再像应用 patch 一样替换 policy 配置。
        await loader.reconcile(base)
        patched = base_bundle(["calculator", "remember", "recall", "uppercase"])
        await loader.reconcile(patched)
        await loader.reconcile([*patched, headless_profile(["大写 cordis"])])

        events = list(root.service(SESSION).events)
        print("=== append-only session log ===")
        for event in events:
            print(json.dumps(event, ensure_ascii=False, sort_keys=True))

        denied = [
            event
            for event in events
            if event["kind"] == "tool_result"
            and event["name"] == "uppercase"
            and not event["result"]["ok"]
        ]
        allowed = [
            event
            for event in events
            if event["kind"] == "tool_result"
            and event["name"] == "uppercase"
            and event["result"]["ok"]
        ]
        assert len(denied) == len(allowed) == 1
        assert any(
            event["kind"] == "tool_result" and event["result"].get("value") == "cordis"
            for event in events
        )

        await loader.close()


if __name__ == "__main__":
    asyncio.run(main())
