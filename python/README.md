# Cordis for Python

Python 推荐用法与旧接口迁移见[Python 接口约定](docs/python-style.md)；TS 行为兼容范围见[对齐记录](docs/ts-parity.md)。

入门文档：[开发者完全指南（入门到深入与 Q&A）](docs/developer-guide.md) · [第一次使用 Cordis](docs/zh-CN.md) · [使用教程](docs/tutorial.md) · [核心概念](docs/core-concepts.md) · [类型化插件开发](docs/typing.md) · [类型化接口迁移](docs/migration-typed-interfaces.md) · [实战：配置自动更新的 CSV 导入](docs/scenarios.md)

进阶参考：[Python vs TS 全方位对比与不足](docs/python-vs-typescript.md) · [核心原理代码走读与维护手册](docs/internals-walkthrough.md) · [API 参考](docs/api.md) · [实现与维护参考](docs/implementation.md) · [与最新 DSH 的差距](docs/cordis-agent-vs-dsh.md) · [Cordis Agent](examples/cordis_agent/) · [离线架构 Demo](examples/dsh_like.py) · [安全卸载 Demo](examples/graceful_unload.py)

本仓库是论文 [*A Programming Paradigm for Spatiotemporal Composability*](https://arxiv.org/abs/2608.25512) 中所描述运行时架构的 Python 实现：

- **依赖驱动的插件生命周期**：声明服务依赖，按可用性启动、更新和退出插件。
- **明确的资源管理**：使用标准上下文管理器获取资源，按作用域和依赖顺序清理。
- **受管异步任务**：在释放资源前等待相关工作完成或响应取消。
- **类型化服务与事件**：通过共享契约检查配置、服务调用、通知参数和查询结果。
- **动态装配与配置更新**：支持插件树、服务隔离、配置持久化和源码热重载。

插件中显式获取依赖，再传给普通 Python 对象；业务代码无需继承服务基类，也无需通过动态属性访问服务。

运行时修复：[2026-09-13 缺陷修复与验证](docs/bug-fixes-2026-09-13.md)（审计 10 项已修复，含取消、并发和回滚回归）。
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


@dataclass(frozen=True)
class GreeterConfig:
    prefix: str


@plugin(provide=[GREETER])
def greeter(ctx: Context, config: GreeterConfig) -> None:
    def greet(name: str) -> str:
        return f"{config.prefix} {name}"

    ctx.provide(GREETER, greet)


@plugin(inject=[GREETER])
def app(ctx: Context, _config: None) -> None:
    greet = ctx.service(GREETER)
    print(greet("Cordis"))


async def main() -> None:
    async with Context() as root:
        consumer = root.use(app)
        provider = root.use(greeter, GreeterConfig(prefix="你好"))
        await root.flush()
        await provider
        await consumer


asyncio.run(main())
```

插件推荐使用返回 `None` 的普通函数或异步函数。通过 `ctx.enter_context()` / `ctx.enter_async_context()` 获取资源，通过 `ctx.defer()` 登记已有清理函数，局部生命周期使用 `async with ctx.scope()`。部分初始化失败时，运行时清理已经登记的资源。服务读取需要匹配已声明的依赖，`ctx.get()` 提供非严格查询；旧的初始化返回值协议仍兼容，见[Python 接口约定](docs/python-style.md)。

`@plugin(inject={"database": metadata}, provide=[...], validate=...)` 支持依赖拦截、服务提供检查以及同步/异步配置校验。默认的 `Fiber.update()` 在新配置启动失败时尝试恢复旧配置；自定义原地更新 hook 需要自行负责回滚。

需要静态类型保证时，使用 `ServiceKey[T]` 代替字符串服务名，并为插件配置声明 dataclass 或 TypedDict。`ctx.use()` 与 `fiber.update()` 会检查配置类型；字符串读取和配置文件继续作为动态接口。完整可运行示例见 [`examples/demo.py`](examples/demo.py)，类型契约和边界见[类型化插件开发](docs/typing.md)。

Loader 也支持 `await loader.mount("worker", worker.configure(config))`，返回保留配置类型的 Fiber；外部配置先通过 `await worker.decode(raw, decoder)` 转换。状态查询返回不暴露 update 的 FiberView。类型化挂载、通知和查询的完整示例：[`examples/typed_interfaces.py`](examples/typed_interfaces.py)，运行 `uv run python -m examples.typed_interfaces`。

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

需要运行时编辑并写回配置时，使用 `ConfigStore.file(loader, path)`；它以不可变 `EntryTree` 执行路径 CRUD，写入前检查外部修改，并将 Loader 更新与原子文件替换作为一个可回滚事务。多个配置文件可通过 `ConfigIncludes(loader).include_file(name, path)` 各自挂到独立 group，局部条目 id 互不冲突。

## 服务与运行时辅助层

服务推荐使用普通对象，在插件装配处通过 `ctx.service(KEY)` 获取依赖，再以构造参数或方法参数传入。为调用方启动任务时，显式接收它的 Context，明确工作归属。

`Service` 的自动注册、调用方 Context 传播以及 `accessor/mixin` 动态属性继续作为兼容能力提供。`ctx.logger`、`ctx.timer` 和 `ctx.registry` 是明确声明的辅助属性，分别用于日志、受管定时任务和插件管理。

## 事件派发

生命周期绑定的事件监听器支持同步派发 `emit`、并发聚合 `parallel`、短路中断 `serial`/`bail` 以及中间件风格的 `waterfall`。内置 internal 事件暴露状态、异常、服务变化、配置更新与派发钩子；`ctx.fibers` 与 `ctx.errors` 提供运行时的快照信息。

业务通知可用 `EventKey[*Args]` 与 on/once、emit/parallel 关联零到多个参数，例如 `EventKey[int, str]`；单处理器请求用 `QueryKey[Request, Result]` 与 handle/query，查询返回 Result 或 None。两种键按对象身份匹配，需由注册方和调用方共享。字符串事件保留原有动态派发策略。

## 异步任务与插件卸载

```python
@plugin(inject=["database"])
def worker(ctx, _config):
    database = ctx.service("database")

    async def execute(job):
        await database.execute(job)

    # 异步回调自动归属注册它的插件；卸载时最多等待 30 秒，再请求取消。
    ctx.on("job", execute, timeout=30)

    # 主动启动的后台任务使用同一套生命周期管理，返回普通 asyncio.Task。
    ctx.spawn(execute("warmup"), policy="cancel", name="warmup")
```

`policy="drain"`（默认）等待任务完成；`policy="cancel"` 在卸载时立即请求取消。`timeout` 是卸载阶段的 drain 宽限期，不是任务执行时限。异步监听器用 `task_policy` 设置相同策略。插件停止接收新任务和事件后，运行时先等待在途任务及其 `finally`，再按登记顺序逆序清理资源；依赖服务在消费者退出后才关闭。局部 `ctx.scope()` 同样管理其中的任务和监听器。

任务结果可以直接 `await`；异常自动带上所属 Fiber 写入 `ctx.errors` 并触发 `internal/error`。`fiber.tasks` 提供未完成任务的快照；`ctx.flush()` 只等待生命周期转换。取消是协作式的：任务必须响应 `CancelledError`，不应阻塞事件循环。裸 `asyncio.create_task()` 不会自动纳管。请从宿主发起卸载；受管任务不能等待自身或依赖的卸载，否则会形成自等待。

可直接运行无需网络的完整示例：

```bash
cd python
PYTHONPATH=src python3 examples/graceful_unload.py
```

## 边界与限制

根 Context 默认限制 1024 个未完成受管任务、保留最近 256 条错误；可用 `Context(max_tasks=..., error_history_limit=...)` 调整。宿主在接收请求前调用 `await root.wait_ready(timeout=...)`，在退出时调用 `await root.close(timeout=...)`。关闭默认等待 30 秒；超时返回阻塞组件和任务快照，后台继续安全清理。新默认值及故障处理方式见[生产运行约定](docs/production.md)。

自动清理覆盖运行时已登记的资源与工作；业务状态的恢复需要明确的补偿措施。对于直接修改进程级全局变量、泄漏线程或丢弃系统资源句柄的 Python 代码，系统无法进行自动恢复。此外，Python 不会卸载已被其他地方引用的字节码对象；HMR（热重载）替换的是模块缓存条目和组件 Fiber，而非解释器进程本身。

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
