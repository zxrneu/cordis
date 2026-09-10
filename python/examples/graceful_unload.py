"""演示依赖感知重载，以及卸载前排空正在使用服务的任务。"""

from __future__ import annotations

import asyncio
from typing import Any

from cordis import Context, Entry, FiberState, Loader, plugin


class Database:
    def __init__(self, version: str, timeline: list[str]):
        self.version = version
        self.timeline = timeline
        self.closed = False

    async def query(self, job: str) -> str:
        if self.closed:
            raise RuntimeError("database 已关闭")
        self.timeline.append(f"{self.version}:{job}:开始")
        await asyncio.sleep(0.05)
        if self.closed:
            raise RuntimeError("database 在查询期间被关闭")
        self.timeline.append(f"{self.version}:{job}:完成")
        return f"{self.version}/{job}"

    async def close(self) -> None:
        self.closed = True
        self.timeline.append(f"{self.version}:数据库关闭")


def make_database(timeline: list[str]):
    @plugin(provide=["database"])
    def database(ctx: Context, config: dict[str, Any]):
        service = Database(config["version"], timeline)
        ctx.provide("database", service)
        timeline.append(f"{service.version}:数据库启动")
        return service.close

    return database


def make_worker(timeline: list[str]):
    @plugin(inject=["database"])
    def worker(ctx: Context, _config: Any):
        version = ctx.database.version
        timeline.append(f"{version}:worker 启动")

        async def execute(job: str) -> None:
            result = await ctx.database.query(job)
            timeline.append(f"结果:{result}")

        # 异步监听器自动归属于 worker；卸载时停止接收新事件，等待在途
        # 查询结束。超过一秒则请求取消，并等待 finally 完成后释放资源。
        ctx.on("job", execute, timeout=1)
        return lambda: timeline.append(f"{version}:worker 停止")

    return worker


async def main() -> None:
    timeline: list[str] = []
    root = Context()
    loader = Loader(root)
    database = make_database(timeline)
    worker = make_worker(timeline)

    def entries(version: str) -> list[Entry]:
        # 同名 Realm 让两个 Entry 解析到同一份 database。
        realm = {"database": "demo"}
        return [
            Entry(
                "database",
                database,
                {"version": version},
                isolate=realm,
            ),
            Entry("worker", worker, isolate=realm),
        ]

    await loader.reconcile(entries("v1"))
    assert loader.get("worker").state is FiberState.ACTIVE

    root.emit("job", "A")
    root.emit("job", "B")
    await asyncio.sleep(0.01)

    # database 配置变化使提供者重载。worker 会先停止接单并排空 A/B；
    # committed view 让查询在 worker teardown 期间仍能访问 v1。
    await loader.reconcile(entries("v2"))
    assert loader.get("worker").state is FiberState.ACTIVE

    root.emit("job", "C")
    await asyncio.sleep(0.06)
    await loader.close()
    await root.close()

    for line in timeline:
        print(line)

    first_close = timeline.index("v1:数据库关闭")
    assert timeline.index("v1:A:完成") < first_close
    assert timeline.index("v1:B:完成") < first_close
    assert timeline.index("v1:worker 停止") < first_close
    assert "结果:v2/C" in timeline


if __name__ == "__main__":
    asyncio.run(main())
