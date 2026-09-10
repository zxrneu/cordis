# Cordis for Python

入门文档：[开发者完全指南（入门到深入与 Q&A）](docs/developer-guide.md) · [第一次使用 Cordis](docs/zh-CN.md) · [使用教程](docs/tutorial.md) · [核心概念](docs/core-concepts.md) · [类型化插件开发](docs/typing.md) · [实战：配置自动更新的 CSV 导入](docs/scenarios.md)

进阶参考：[Python vs TS 全方位对比与不足](docs/python-vs-typescript.md) · [核心原理代码走读与维护手册](docs/internals-walkthrough.md) · [API 参考](docs/api.md) · [实现与维护参考](docs/implementation.md) · [与最新 DSH 的差距](docs/cordis-agent-vs-dsh.md) · [Cordis Agent](examples/cordis_agent/) · [离线架构 Demo](examples/dsh_like.py) · [安全卸载 Demo](examples/graceful_unload.py)

本仓库是论文 [*A Programming Paradigm for Spatiotemporal Composability*](https://arxiv.org/abs/2608.25512) 中所描述运行时架构的 Python 实现：

- **增量可逆 Effect 迭代器**：增量注册逆操作（Inverse），并在卸载时严格按后进先出（LIFO）顺序恢复；
- **独立的惯性 `Fiber` 生命周期**：每个组件拥有独立的生命周期控制与异步惯性状态机；
- **响应式 Coeffect**：声明的 Coeffect（依赖与条件）以响应式方式激活和停用组件；
- **一致的依赖视图**：已提交的依赖视图在完整的卸载（Teardown）流程中保持可读；
- **统一的生命周期归属**：子组件、事件、服务及嵌套 Effect 共享生命周期所有权；
- **受管异步任务**：`ctx.spawn()` 与异步事件回调跟随所属插件或嵌套 Effect，在资源回收前排空或取消；
- **类型化 API**：泛型 `Component` / `Fiber` 保留配置与 Context 类型，`ServiceKey[T]` 检查服务注册和读取，Context 描述符支持属性补全；
- **隔离域与拦截机制**：Context 支持隔离 Realm 与动态合并的拦截元数据；
- **声明式加载器**：基于键控配置树与受管 Realm 进行协调管理（Reconciliation）；
- **感知依赖的 HMR（热重载）**：事务性替换模块，在导入或激活失败时自动回滚至上一可用组件状态。

## 快速上手

Python 3.11+，从仓库根目录运行：

```bash
cd python
uv sync
uv run python -m examples.demo
```

下面是类型完整的最小示例。包含配置更新、依赖等待和卸载顺序的完整程序可用 `uv run python -m examples.tutorial` 运行，逐步说明见[使用教程](docs/tutorial.md)。

```python
import asyncio
from dataclasses import dataclass
from typing import Protocol

from cordis import Context, ServiceKey, plugin


class Greeter(Protocol):
    def __call__(self, name: str, /) -> str: ...


GREETER = ServiceKey[Greeter]("greeter")


class AppContext(Context):
    greeter = GREETER


@dataclass(frozen=True)
class GreeterConfig:
    prefix: str


@plugin(provide=[GREETER])
def greeter(ctx: AppContext, config: GreeterConfig) -> None:
    def greet(name: str) -> str:
        return f"{config.prefix} {name}"

    ctx.provide(GREETER, greet)


@plugin(inject=[GREETER])
def app(ctx: AppContext, _config: None) -> None:
    print(ctx.greeter("Cordis"))


async def main() -> None:
    async with AppContext() as root:
        consumer = root.use(app)
        provider = root.use(greeter, GreeterConfig(prefix="你好"))
        await root.flush()
        await provider
        await consumer


asyncio.run(main())
```

组件支持同步或异步函数、生成器，以及具有 `start`/`stop` 生命周期的对象；初始化还可以返回上下文管理器。组件可返回清理回调，通过 `yield` 逐步登记清理，或通过 `await ctx.effect(...)` 创建嵌套 Effect。部分初始化失败时，运行时撤销已经登记的操作。服务读取需要匹配已声明的依赖，`ctx.get()` 提供非严格的反射式查询；两者区别见[核心概念](docs/core-concepts.md)。

`@plugin(inject={"database": metadata}, provide=[...], validate=...)` 支持依赖拦截、服务提供检查以及同步/异步配置校验。默认的 `Fiber.update()` 在新配置启动失败时尝试恢复旧配置；自定义原地更新 hook 需要自行负责回滚。

需要静态类型保证时，使用 `ServiceKey[T]` 代替字符串服务名，并为插件配置声明 dataclass 或 TypedDict。`ctx.use()` 与 `fiber.update()` 会检查配置类型；字符串读取和配置文件继续作为动态接口。完整可运行示例见 [`examples/demo.py`](examples/demo.py)，类型契约和边界见[类型化插件开发](docs/typing.md)。

## 声明式加载器

想看配置文件修改后自动生效的完整应用，可以运行 `uv run python -m examples.order_importer --demo`。它将服务契约、提供方、调用方分开，通过修改 TOML 更新金额规则，并在 SQLite 与内存存储间切换；[实战文档](docs/scenarios.md)包含常驻监听、三角色分工和增加提供方的步骤。

```python
from cordis import Entry, Loader

loader = Loader(ctx)
await loader.reconcile([
    Entry("api", "my_app.plugins:api", {"port": 8080},
          isolate={"database": "tenant-a"}),
    Entry("jobs", "my_app.plugins:jobs",
          intercept={"database": {"readonly": True}}),
    Entry("workers", children=(
        Entry("email", "my_app.plugins:email_worker"),
        Entry("index", "my_app.plugins:index_worker"),
    )),
])

await loader.reload("api")
await loader.reload_changed(["my_app/plugins/shared.py"])
```

插件定位符使用 `package.module:attribute` 语法。`load_file()` 支持 JSON 和 TOML；在安装可选依赖（`pip install 'cordis-python[yaml]'`）后还支持 YAML。`watch_file()` 和 `HotReloader.watch()` 提供了无外部依赖的轮询式文件监听器。

## 事件派发

生命周期绑定的事件监听器支持同步派发 `emit`、并发聚合 `parallel`、短路中断 `serial`/`bail` 以及中间件风格的 `waterfall`。内置的 `internal/status` 和 `internal/error` 事件用于暴露生命周期状态转换和异常；`ctx.fibers` 与 `ctx.errors` 提供运行时的快照信息。

## 异步任务与插件卸载

```python
@plugin(inject=["database"])
def worker(ctx, _config):
    async def execute(job):
        await ctx.database.execute(job)

    # 异步回调自动归属注册它的插件；卸载时最多等待 30 秒，再请求取消。
    ctx.on("job", execute, timeout=30)

    # 主动启动的后台任务使用同一套生命周期管理，返回普通 asyncio.Task。
    ctx.spawn(execute("warmup"), policy="cancel", name="warmup")
```

`policy="drain"`（默认）等待任务完成；`policy="cancel"` 在卸载时立即请求取消。`timeout` 是卸载阶段的 drain 宽限期，不是任务执行时限。异步监听器用 `task_policy` 设置相同策略。插件停止接收新任务和事件后，运行时先等待在途任务及其 `finally`，再按 LIFO 撤销 Effect；依赖服务在消费者退出后才关闭。嵌套 `ctx.effect()` 同样拥有其中的任务和监听器。

任务结果可以直接 `await`；异常自动带上所属 Fiber 写入 `ctx.errors` 并触发 `internal/error`。`fiber.tasks` 提供未完成任务的快照；`ctx.flush()` 只等待生命周期转换。取消是协作式的：任务必须响应 `CancelledError`，不应阻塞事件循环。裸 `asyncio.create_task()` 不会自动纳管。请从宿主发起卸载；受管任务不能等待自身或依赖的卸载，否则会形成自等待。

可直接运行无需网络的完整示例：

```bash
cd python
PYTHONPATH=src python3 examples/graceful_unload.py
```

## 边界与限制

时间维度的恢复仅适用于通过上下文执行的操作，或显式配对了逆操作的操作。对于直接修改进程级全局变量、泄漏线程或丢弃系统资源句柄的 Python 代码，系统无法进行自动恢复。此外，Python 不会卸载已被其他地方引用的字节码对象；HMR（热重载）替换的是模块缓存条目和组件 Fiber，而非解释器进程本身。

## 本地开发

```bash
cd python
uv sync
uv run mypy
uv run python -m unittest discover -v
uv run ruff check src tests
```

## 构建与发布

在每次发布前，请先更新 `pyproject.toml` 中的 `project.version`。PyPI 不允许覆盖已存在的版本。

执行检查并同时构建源码分发包（sdist）和二进制包（wheel）：

```bash
cd python
uv sync
uv run mypy
uv run python -m unittest discover -v
uv run ruff check src tests
uv build --clear
uv publish --dry-run
```

打包生成的安装文件会输出到 `dist/` 目录。在终端环境或 CI Secret 中配置 `UV_PUBLISH_TOKEN` 后，即可发布至 PyPI：

```bash
uv publish
```
