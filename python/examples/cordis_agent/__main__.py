from __future__ import annotations

import argparse
import asyncio
import os
from pathlib import Path

from cordis import Context, Loader

from .plugins import bundle


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="可连接真实 DeepSeek API 的 Cordis Agent。"
    )
    parser.add_argument("--workspace", default=os.getcwd(), help="工具可访问的根目录")
    parser.add_argument(
        "--session",
        default=".cordis-agent/session.jsonl",
        help="append-only 会话日志路径",
    )
    parser.add_argument("--resume", action="store_true", help="恢复已有 session")
    parser.add_argument(
        "--once", action="append", default=[], help="执行一条指令后退出，可重复"
    )
    parser.add_argument(
        "--api-key",
        default=os.environ.get("DEEPSEEK_API_KEY", ""),
        help="默认读取 DEEPSEEK_API_KEY",
    )
    parser.add_argument(
        "--base-url",
        default=os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
    )
    parser.add_argument(
        "--model",
        default=os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-flash"),
    )
    parser.add_argument("--thinking", action="store_true", help="启用 thinking mode")
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--max-steps", type=int, default=12)
    args = parser.parse_args()
    if not args.api_key:
        parser.error("请设置 DEEPSEEK_API_KEY，或传入 --api-key")
    if args.timeout <= 0 or args.max_steps <= 0:
        parser.error("timeout 和 max-steps 必须大于 0")
    return args


async def main() -> None:
    args = parse_args()
    args.workspace, args.session = await asyncio.gather(
        asyncio.to_thread(lambda: str(Path(args.workspace).resolve())),
        asyncio.to_thread(lambda: str(Path(args.session).resolve())),
    )
    async with Context() as root:
        loader = Loader(root)
        try:
            await loader.reconcile(bundle(args))
        finally:
            await loader.close()


if __name__ == "__main__":
    asyncio.run(main())
