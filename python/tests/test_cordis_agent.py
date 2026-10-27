import asyncio
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from cordis import Context, Loader, plugin
from examples.cordis_agent import DeepSeekAPI, Workspace, bundle
from examples.cordis_agent import llm as agent_llm
from examples.cordis_agent.plugins import memory_tools_plugin
from examples.cordis_agent.tools import ApprovalPolicy, ToolRuntime
from examples.cordis_agent.contracts import Tool


class AsyncWriter:
    def __init__(self):
        self.data = bytearray()

    def write(self, data: bytes) -> None:
        self.data.extend(data)

    async def drain(self) -> None:
        pass

    def close(self) -> None:
        pass

    async def wait_closed(self) -> None:
        pass


class CordisAgentDemoTests(unittest.IsolatedAsyncioTestCase):
    async def test_partial_tool_registration_is_rolled_back(self):
        @plugin(provide=["tools", "memory", "policy"])
        def provider(ctx, _config):
            policy = ApprovalPolicy(interactive=False)
            ctx.provide("policy", policy)
            tools = ToolRuntime(policy)
            ctx.provide("tools", tools)
            ctx.provide("memory", {})
            tools.register(Tool("recall", "existing", {}, lambda _: None))

        async with Context() as root:
            await root.use(provider)
            with self.assertRaisesRegex(ValueError, "工具重复注册"):
                await root.use(memory_tools_plugin)
            self.assertEqual(
                [item["function"]["name"] for item in root.service("tools").schemas()],
                ["recall"],
            )

    async def test_deepseek_chat_completion_wire_protocol(self):
        body = json.dumps(
            {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call-1",
                                    "type": "function",
                                    "function": {
                                        "name": "calculator",
                                        "arguments": '{"expression":"6*7"}',
                                    },
                                }
                            ],
                        }
                    }
                ]
            }
        ).encode()
        reader = asyncio.StreamReader()
        reader.feed_data(
            b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
            + f"Content-Length: {len(body)}\r\n\r\n".encode()
            + body
        )
        reader.feed_eof()
        writer = AsyncWriter()

        async def open_connection(*_args, **_kwargs):
            return reader, writer

        client = DeepSeekAPI(
            "test-key",
            base_url="http://127.0.0.1:8000",
            model="deepseek-v4-flash",
            thinking=False,
            timeout=1,
        )
        with patch.object(agent_llm.asyncio, "open_connection", new=open_connection):
            message = await client.complete(
                [{"role": "user", "content": "计算 6*7"}],
                [
                    {
                        "type": "function",
                        "function": {
                            "name": "calculator",
                            "description": "计算",
                            "parameters": {"type": "object"},
                        },
                    }
                ],
            )

        header, raw_payload = bytes(writer.data).split(b"\r\n\r\n", 1)
        payload = json.loads(raw_payload)
        self.assertIn(b"POST /chat/completions HTTP/1.1", header)
        self.assertIn(b"Authorization: Bearer test-key", header)
        self.assertEqual(payload["model"], "deepseek-v4-flash")
        self.assertEqual(payload["thinking"], {"type": "disabled"})
        self.assertEqual(message["tool_calls"][0]["function"]["name"], "calculator")

    async def test_full_pluginized_agent_tool_loop(self):
        async def complete(_client, messages, _tools):
            if messages[-1]["role"] == "tool":
                return {"role": "assistant", "content": "答案是 42"}
            return {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call-42",
                        "type": "function",
                        "function": {
                            "name": "calculator",
                            "arguments": '{"expression":"6*7"}',
                        },
                    }
                ],
            }

        with tempfile.TemporaryDirectory() as directory:
            args = SimpleNamespace(
                session=str(Path(directory) / "session.jsonl"),
                resume=False,
                once=["计算 6*7"],
                workspace=directory,
                api_key="test-key",
                base_url="https://api.deepseek.com",
                model="deepseek-v4-flash",
                thinking=False,
                timeout=1,
                max_steps=4,
            )
            root = Context()
            loader = Loader(root)
            with patch.object(DeepSeekAPI, "complete", new=complete):
                with redirect_stdout(io.StringIO()):
                    await loader.reconcile(bundle(args))

            messages = root.session.messages
            tool_message = next(item for item in messages if item["role"] == "tool")
            self.assertEqual(json.loads(tool_message["content"])["value"], 42)
            self.assertEqual(messages[-1]["content"], "答案是 42")
            await loader.close()
            await root.close()

    async def test_workspace_cannot_escape_its_root(self):
        workspace = await Workspace.open(Path(__file__).parents[1])
        with self.assertRaises(PermissionError):
            await workspace.resolve("../outside.txt")


if __name__ == "__main__":
    unittest.main()
