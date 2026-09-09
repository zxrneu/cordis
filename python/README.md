# Cordis for Python

相关文档：[从原理、实现到应用](docs/zh-CN.md) · [与最新 DSH 的差距](docs/cordis-agent-vs-dsh.md) · [Cordis Agent](examples/cordis_agent/) · [离线架构 Demo](examples/dsh_like.py) · [安全卸载 Demo](examples/graceful_unload.py)

本仓库是论文 [*A Programming Paradigm for Spatiotemporal Composability*](https://arxiv.org/abs/2608.25512) 中所描述运行时架构的 Python 实现：

- **增量可逆 Effect 迭代器**：增量注册逆操作（Inverse），并在卸载时严格按后进先出（LIFO）顺序恢复；
- **独立的惯性 `Fiber` 生命周期**：每个组件拥有独立的生命周期控制与异步惯性状态机；
- **响应式 Coeffect**：声明的 Coeffect（依赖与条件）以响应式方式激活和停用组件；
- **一致的依赖视图**：已提交的依赖视图在完整的卸载（Teardown）流程中保持可读；
- **统一的生命周期归属**：子组件、事件、服务及嵌套 Effect 共享生命周期所有权；
- **隔离域与拦截机制**：Context 支持隔离 Realm 与动态合并的拦截元数据；
- **声明式加载器**：基于键控配置树与受管 Realm 进行协调管理（Reconciliation）；
- **感知依赖的 HMR（热重载）**：事务性替换模块，在导入或激活失败时自动回滚至上一可用组件状态。

## 快速上手

```python
import asyncio
from cordis import Context, plugin


@plugin(provide=["greeter"])
def greeter(ctx, config):
    ctx.provide("greeter", lambda name: f"{config['prefix']} {name}")


@plugin(inject=["greeter"])
def app(ctx, _config):
    print(ctx.greeter("world"))
    ctx.on("hello", lambda name: print(ctx.greeter(name)))


async def main():
    async with Context() as ctx:
        ctx.use(app)  # 保持挂起（Pending），直到依赖就绪
        ctx.use(greeter, {"prefix": "hello"})
        await ctx.flush()
        ctx.emit("hello", "Cordis")


asyncio.run(main())
```

组件可以是同步或异步函数、生成器、带有 `start`/`stop` 方法的类，或者上下文管理器（Context Manager）。组件可返回一个清理回调函数、通过 `yield` 产出多个清理回调，或通过 `await ctx.effect(...)` 注册任意嵌套 Effect。在生成器步进到下一步之前，每一个已产出的逆操作都会被跟踪记录，因此即使组件发生部分初始化失败也能被完整回滚恢复。仅当插件在 `inject` 中声明了 `database` 时，才允许通过 `ctx.database` 或 `ctx["database"]` 访问；`ctx.get("database")` 则提供非严格的反射式查询。

`@plugin(inject={"database": metadata}, provide=[...], validate=...)` 支持依赖拦截、服务提供检查以及同步/异步配置校验。当更新后的配置应用失败时，`Fiber.update()` 会自动回滚到上一个正常运行的配置版本。

## 声明式加载器

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

## 边界与限制

时间维度的恢复仅适用于通过上下文执行的操作，或显式配对了逆操作的操作。对于直接修改进程级全局变量、泄漏线程或丢弃系统资源句柄的 Python 代码，系统无法进行自动恢复。此外，Python 不会卸载已被其他地方引用的字节码对象；HMR（热重载）替换的是模块缓存条目和组件 Fiber，而非解释器进程本身。

## 本地开发

```bash
cd python
uv sync
uv run python -m unittest discover -v
uv run ruff check src tests
```

## 构建与发布

在每次发布前，请先更新 `pyproject.toml` 中的 `project.version`。PyPI 不允许覆盖已存在的版本。

执行检查并同时构建源码分发包（sdist）和二进制包（wheel）：

```bash
cd python
uv sync
uv run python -m unittest discover -v
uv run ruff check src tests
uv build --clear
uv publish --dry-run
```

打包生成的安装文件会输出到 `dist/` 目录。在终端环境或 CI Secret 中配置 `UV_PUBLISH_TOKEN` 后，即可发布至 PyPI：

```bash
uv publish
```
