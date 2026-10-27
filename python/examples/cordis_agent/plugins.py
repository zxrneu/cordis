from __future__ import annotations

import asyncio
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from cordis import Context, Entry, plugin

from .agent import AgentRuntime
from .contracts import Json, Tool, object_schema, required_string
from .keys import AGENT, MEMORY, MODEL, POLICY, SESSION, SYSTEM_PROMPT, TOOLS
from .llm import DeepSeekAPI
from .session import SessionLog
from .tools import ApprovalPolicy, ToolRuntime, Workspace, calculate


@plugin(provide=[SESSION])
async def session_plugin(ctx: Context, config: Json) -> None:
    ctx.provide(
        SESSION,
        await SessionLog.open(Path(config["path"]), resume=config["resume"]),
    )


@plugin(provide=[POLICY])
def policy_plugin(ctx: Context, config: Json) -> None:
    ctx.provide(POLICY, ApprovalPolicy(interactive=config["interactive"]))


@plugin(inject=[POLICY], provide=[TOOLS])
def tools_plugin(ctx: Context, _config: None) -> None:
    ctx.provide(TOOLS, ToolRuntime(ctx.service(POLICY)))


@plugin(inject=[TOOLS])
def calculator_plugin(ctx: Context, _config: None) -> None:
    schema = object_schema(
        {"expression": {"type": "string", "description": "四则运算表达式"}},
        ["expression"],
    )
    ctx.defer(
        ctx.service(TOOLS).register(
            Tool("calculator", "安全计算四则表达式", schema, calculate)
        )
    )


@plugin(inject=[TOOLS])
async def workspace_plugin(ctx: Context, config: Json) -> None:
    workspace = await Workspace.open(Path(config["root"]))
    path_schema = object_schema(
        {"path": {"type": "string", "description": "workspace 内相对路径"}},
        ["path"],
    )
    write_schema = object_schema(
        {
            "path": {"type": "string", "description": "workspace 内相对路径"},
            "content": {"type": "string", "description": "完整文件内容"},
        },
        ["path", "content"],
    )
    for tool in (
        Tool(
            "list_files",
            "列出 workspace 文件，最多 200 个",
            path_schema,
            workspace.list_files,
        ),
        Tool(
            "read_file",
            "读取 workspace 内小于 100 KB 的 UTF-8 文件",
            path_schema,
            workspace.read_file,
        ),
        Tool(
            "write_file",
            "写入 workspace 文件；执行前需要用户批准",
            write_schema,
            workspace.write_file,
            mutating=True,
        ),
    ):
        ctx.defer(ctx.service(TOOLS).register(tool))


@plugin(provide=[MEMORY])
def memory_plugin(ctx: Context, _config: None) -> None:
    ctx.provide(MEMORY, {})


@plugin(inject=[TOOLS, MEMORY])
def memory_tools_plugin(ctx: Context, _config: None) -> None:
    schema = object_schema(
        {
            "key": {"type": "string"},
            "value": {"type": "string"},
        },
        ["key", "value"],
    )
    recall_schema = object_schema({"key": {"type": "string"}}, ["key"])
    memory = ctx.service(MEMORY)

    def remember(arguments: Mapping[str, object]) -> str:
        key = required_string(arguments, "key")
        memory[key] = required_string(arguments, "value", allow_empty=True)
        return f"已记住 {key}"

    def recall(arguments: Mapping[str, object]) -> str:
        return memory.get(required_string(arguments, "key"), "未找到")

    for tool in (
        Tool("remember", "保存当前进程内记忆", schema, remember),
        Tool("recall", "读取当前进程内记忆", recall_schema, recall),
    ):
        ctx.defer(ctx.service(TOOLS).register(tool))


@plugin(provide=[SYSTEM_PROMPT])
def prompt_plugin(ctx: Context, config: Json) -> None:
    ctx.provide(SYSTEM_PROMPT, config["text"])


@plugin(provide=[MODEL])
def llm_plugin(ctx: Context, config: Json) -> None:
    ctx.provide(MODEL, DeepSeekAPI(**config))


@plugin(
    inject=[MODEL, TOOLS, SESSION, SYSTEM_PROMPT],
    provide=[AGENT],
)
def agent_plugin(ctx: Context, config: Json) -> None:
    ctx.provide(
        AGENT,
        AgentRuntime(
            ctx.service(SESSION),
            ctx.service(MODEL),
            ctx.service(TOOLS),
            ctx.service(SYSTEM_PROMPT),
            max_steps=config["max_steps"],
        ),
    )


@plugin(inject=[AGENT])
async def headless_plugin(ctx: Context, config: Json) -> None:
    agent = ctx.service(AGENT)
    prompts = config["prompts"]
    if prompts:
        for prompt in prompts:
            print(await agent.run(prompt))
        return

    print("Cordis Agent 已启动。输入 /exit 退出。")
    while True:
        try:
            prompt = await asyncio.to_thread(input, "\n你> ")
        except EOFError:
            break
        if prompt.strip() in {"/exit", "/quit"}:
            break
        if prompt.strip():
            print(f"\n助手> {await agent.run(prompt)}")


def bundle(args: Any) -> list[Entry]:
    prompt = (
        "你是运行在本地 workspace 中的编程助手。需要事实时使用工具；"
        "不得假装已经读取或写入文件。修改文件前说明意图。"
    )
    return [
        Entry(
            "session",
            session_plugin,
            {"path": args.session, "resume": args.resume},
        ),
        Entry("policy", policy_plugin, {"interactive": not args.once}),
        Entry("tools", tools_plugin),
        Entry("tool/calculator", calculator_plugin),
        Entry("tool/workspace", workspace_plugin, {"root": args.workspace}),
        Entry("memory", memory_plugin),
        Entry("tool/memory", memory_tools_plugin),
        Entry("prompt", prompt_plugin, {"text": prompt}),
        Entry(
            "llm",
            llm_plugin,
            {
                "api_key": args.api_key,
                "base_url": args.base_url,
                "model": args.model,
                "thinking": args.thinking,
                "timeout": args.timeout,
            },
        ),
        Entry("agent", agent_plugin, {"max_steps": args.max_steps}),
        Entry("app/headless", headless_plugin, {"prompts": args.once}),
    ]
