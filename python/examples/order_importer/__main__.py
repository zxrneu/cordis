from __future__ import annotations

import argparse
import asyncio
import tempfile
from pathlib import Path

from cordis import Context

from .application import Application
from .definitions import STORE, ImportReport
from .money import format_cents


async def serve(path: Path) -> None:
    async with Context() as root:
        app = Application(root, path)
        await app.refresh()
        print(f"正在监听 {app.path}，修改配置或 CSV 后保存；Ctrl+C 退出。", flush=True)
        await root.spawn(app.watch(), policy="cancel", name="orders:config")


async def demo() -> None:
    """Exercise the same file watcher, without requiring manual edits."""
    with tempfile.TemporaryDirectory(prefix="cordis-orders-") as directory:
        target = Path(directory)
        path = target / "config.toml"
        # Use private fixtures: manual edits to the example do not affect --demo.
        initial = """[database]
provider = "sqlite"
path = "var/orders.sqlite3"
[validation]
max_amount = "50.00"
[importer]
directory = "incoming"
interval = 0.1
[logging]
enabled = true
"""
        path.write_text(initial, encoding="utf-8")
        incoming = target / "incoming"
        incoming.mkdir()
        (incoming / "orders.csv").write_text(
            "order_id,customer,amount\n"
            "ORD-001,Alice,25.00\nORD-002,Bob,80.00\nORD-003,Carol,-5.00\n",
            encoding="utf-8",
        )
        async with Context() as root:
            reports: asyncio.Queue[ImportReport] = asyncio.Queue()
            root.on("orders/imported", reports.put_nowait)
            app = Application(root, path)
            await app.refresh()
            root.spawn(app.watch(), policy="cancel", name="orders:config")
            first = await asyncio.wait_for(reports.get(), timeout=5)
            assert first.accepted == 1 and len(first.rejected) == 2

            print("\n[演示] 将配置文件中的金额上限改为 100.00", flush=True)
            path.write_text(
                initial.replace('max_amount = "50.00"', 'max_amount = "100.00"'),
                encoding="utf-8",
            )
            second = await asyncio.wait_for(reports.get(), timeout=5)
            assert second.accepted == 2 and len(second.rejected) == 1
            rows = root.service(STORE).all()
            assert len(rows) == 2
            print("\n[数据库] 重复导入按订单号更新，当前共 2 条：", flush=True)
            for order in rows:
                print(
                    f"  {order.order_id} {order.customer} {format_cents(order.amount_cents)}"
                )
            old_store = root.service(STORE)
            print("\n[演示] 将 database.provider 从 sqlite 改成 memory", flush=True)
            path.write_text(
                initial.replace(
                    'max_amount = "50.00"', 'max_amount = "100.00"'
                ).replace('provider = "sqlite"', 'provider = "memory"'),
                encoding="utf-8",
            )
            third = await asyncio.wait_for(reports.get(), timeout=5)
            assert third.accepted == 2 and root.service(STORE) is not old_store
            assert root.service(STORE).all() == rows
            print("[替换成功] 内存提供方使用相同契约，导入与日志消费者保持原代码。")
        print("[退出] 配置监听、目录扫描和数据库连接已清理。")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Cordis CSV 订单导入与配置自动更新示例"
    )
    parser.add_argument(
        "--demo", action="store_true", help="在临时目录演示规则更新和存储提供方切换"
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=Path(__file__).with_name("config.toml"),
        help="TOML 配置路径；默认使用示例自带配置",
    )
    args = parser.parse_args()
    try:
        asyncio.run(demo() if args.demo else serve(args.config))
    except KeyboardInterrupt:
        print("\n已停止。")


if __name__ == "__main__":
    main()
