# 使用教程：从类型化插件到动态装配

这份教程面向本仓库的 Python 实现，要求 Python 3.11 或更新版本。我们会做一个问候应用：`greeter` 插件提供服务，`app` 插件消费服务并响应事件，然后在运行中更新配置、移除服务。

完整程序在 [`examples/tutorial.py`](../examples/tutorial.py)。理解运行机制可读[核心概念](core-concepts.md)，类型推断细节可读[类型化插件开发](typing.md)。

需要实际业务场景和配置文件自动更新，可继续阅读[CSV 订单导入实战](scenarios.md)：它监听 TOML 与 CSV，更新校验规则并写入 SQLite。

## 1. 安装并运行

从仓库根目录执行：

```bash
cd python
uv sync
uv run python -m examples.tutorial
```

以下命令均在 `python/` 目录执行。没有 uv 时，可以使用虚拟环境安装当前源码：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e .
.venv/bin/python -m examples.tutorial
```

运行时核心使用 Python 标准库；mypy、ruff 是开发依赖。这里安装的是当前仓库，不要求 PyPI 上的版本与它一致。

## 2. 定义服务接口和配置

首先描述调用者需要什么能力，并为它定义一个共享的服务键：

```python
from dataclasses import dataclass
from typing import Protocol

from cordis import Context, ServiceKey


class Greeter(Protocol):
    def __call__(self, name: str, /) -> str: ...


GREETER = ServiceKey[Greeter]("greeter")


class AppContext(Context):
    greeter = GREETER


@dataclass(frozen=True)
class GreeterConfig:
    prefix: str
```

`Greeter` 是服务接口，符合签名的函数也能实现它。`GREETER` 把服务名与接口类型关联起来；`AppContext.greeter` 是只读服务描述符，使 `ctx.greeter("Cordis")` 具有参数检查和编辑器补全。

实际项目中，把接口、服务键和 `AppContext` 放到共享的 `contracts.py`，由提供者和消费者共同导入。配置使用不可变 dataclass，更新时创建新配置，避免原地修改导致变更比较失效。

## 3. 编写提供者与消费者

继续在同一文件中定义两个插件：

```python
import asyncio

from cordis import Cleanup, plugin


@plugin(provide=[GREETER])
def greeter(ctx: AppContext, config: GreeterConfig) -> Cleanup:
    def greet(name: str) -> str:
        return f"{config.prefix} {name}"

    ctx.provide(GREETER, greet)
    print(f"服务启动: {config.prefix}")

    def cleanup() -> None:
        print(f"服务清理: {config.prefix}")

    return cleanup


@plugin(inject=[GREETER])
def app(ctx: AppContext, _config: None) -> Cleanup:
    async def on_greet(name: str) -> str:
        await asyncio.sleep(0)
        message = ctx.greeter(name)
        print(message)
        return message

    ctx.on("greet", on_greet, timeout=5)
    print("应用启动")

    def cleanup() -> None:
        print("应用清理")

    return cleanup
```

`provide=[GREETER]` 声明组件承诺提供服务，`ctx.provide()` 注册实际对象。`inject=[GREETER]` 声明组件的启动条件；服务缺失时，`app` 保持 `pending`，函数体尚不会执行。`_config: None` 表示这个插件不需要配置。

返回的 `cleanup` 会在卸载时执行。监听器、服务注册本身已经由 Context 管理，无需在 cleanup 中重复注销。本例用打印展示清理顺序；数据库连接、文件句柄等资源则应在对应清理中关闭。

异步监听器的任务属于注册它的 `app` 插件。这里的 `timeout=5` 表示卸载时最多等待 5 秒自然完成，随后请求取消；它不是每次事件处理的执行时限。

## 4. 装配、派发和更新

把下面的宿主入口接在前面的定义后面，即得到完整可运行程序：

```python
from cordis import FiberState


async def main() -> None:
    async with AppContext() as root:
        consumer = root.use(app)
        assert (await consumer).state is FiberState.PENDING
        print(f"等待依赖: {consumer.state.value}")

        provider = root.use(greeter, GreeterConfig(prefix="你好"))
        await root.flush()
        await provider
        assert (await consumer).state is FiberState.ACTIVE
        await root.parallel("greet", "Cordis")

        print("-- 更新配置 --")
        await provider.update(GreeterConfig(prefix="Hello"))
        await root.flush()
        assert (await consumer).state is FiberState.ACTIVE
        await root.parallel("greet", "Cordis")

        print("-- 移除服务 --")
        await provider.dispose()
        await root.flush()
        assert (await consumer).state is FiberState.PENDING
        print(f"等待依赖: {consumer.state.value}")


if __name__ == "__main__":
    asyncio.run(main())
```

`ctx.use()` 返回的是插件运行实例 `Fiber`，并立即调度启动。`provider` 的类型是 `Fiber[GreeterConfig, AppContext]`，因此 `provider.update()` 也要求 `GreeterConfig`；错误的配置类型会被 mypy 拒绝。

这里有三个不同的等待动作：

| 写法 | 等待什么 |
| --- | --- |
| `await fiber` / `await fiber.wait()` | 该 Fiber 当前的生命周期转换，并抛出它保存的启动错误 |
| `await root.flush()` | 当前运行时中持续产生的生命周期转换，直到没有转换任务 |
| `await root.parallel("greet", "Cordis")` | 这次事件调用的异步监听器完成；方法本身返回 `None` |

`await fiber` 和 `flush()` 都不会一直等到缺失的服务未来出现，也不会等待所有后台任务结束。`flush()` 本身不会把所有插件启动错误自动抛给宿主，所以需要对关心的 Fiber 单独等待，并在要求就绪的地方检查 `state`。

运行结果：

```text
等待依赖: pending
服务启动: 你好
应用启动
你好 Cordis
-- 更新配置 --
应用清理
服务清理: 你好
服务启动: Hello
应用启动
Hello Cordis
-- 移除服务 --
应用清理
服务清理: Hello
等待依赖: pending
```

更新服务配置时，消费者先退出，旧服务再清理，然后新服务和消费者重新启动。移除提供者后，消费者仍在等待依赖；以后重新 `root.use(greeter, ...)` 可以使它再次激活。被 `dispose()` 的提供者自身已经永久退出，不能再 `restart()`。

默认的 `update()` 会在配置改变时重启插件；若新配置启动失败，会尝试恢复旧配置，再抛出错误。自定义 `update` hook 会改为原地更新，其副作用与回滚需要插件自行负责。退出 `async with` 会关闭整个运行时。

## 5. 管理资源和后台任务

有多个资源时，用生成器在每一步获得资源后立即交出清理操作：

```python
from collections.abc import Iterator
from pathlib import Path


@plugin
def audit(ctx: Context, path: Path) -> Iterator[Cleanup]:
    stream = path.open("a", encoding="utf-8")
    yield stream.close

    def write(message: str) -> None:
        stream.write(message + "\n")
        stream.flush()

    ctx.on("audit", write)
```

每次 `yield` 的清理会先注册，再继续初始化。如果后续初始化失败，已经注册的清理仍会执行。卸载时按后进先出顺序撤销：这里先注销监听器，再关闭文件。关闭句柄不会撤销已经写入文件的内容。

主动启动后台任务时使用 `ctx.spawn()`：

```python
@plugin
def ticker(ctx: Context, _config: None) -> None:
    async def loop() -> None:
        try:
            while True:
                await asyncio.sleep(1)
                ctx.emit("audit", "tick")
        finally:
            print("ticker 退出")

    ctx.spawn(loop(), policy="cancel", name="ticker")
```

有限任务可以使用默认的 `policy="drain"`，等待其结束；长期循环通常使用 `policy="cancel"`。运行时会等任务及其 `finally` 完成，再回收资源。裸 `asyncio.create_task()` 不会自动归属插件；取消也要求任务主动响应，不能保证终止忽略取消的协程或底层线程。

`emit()` 会调度异步监听器但立即返回；需要等待处理完成时用 `await parallel()`。后者不返回监听器结果；需要业务返回值时，可直接调用类型化服务，或使用带短路语义的 `serial()`。事件参数目前属于动态接口，即使监听器写了类型注解，字符串事件派发也不会自动检查参数类型。

## 6. 用 Loader 管理插件清单

插件数量增多时，给每个条目一个稳定 `id`，交给 Loader 比较目标配置。以下是独立的宿主示例，直接复用教程模块：

```python
import asyncio

from cordis import Entry, FiberState, Loader
from examples.tutorial import AppContext, GreeterConfig, app, greeter


async def main() -> None:
    async with AppContext() as root:
        loader = Loader(root)
        try:
            for prefix in ("你好", "Hello"):
                await loader.reconcile([
                    Entry("greeter", greeter, GreeterConfig(prefix=prefix)),
                    Entry("app", app),
                ])
                for entry_id in ("greeter", "app"):
                    fiber = loader.get(entry_id)
                    assert fiber is not None
                    await fiber
                    assert fiber.state is FiberState.ACTIVE
                await root.parallel("greet", "Cordis")
        finally:
            await loader.close()


asyncio.run(main())
```

清单中新增的 id 会挂载，移除的 id 会卸载，已有 id 的配置变化会触发更新；`enabled=False` 可保留条目但停用实例。一次 `reconcile()` 是逐项协调，并非整个清单的原子事务。

`Entry` 容纳不同类型的组件，配置类型为 `object`，不保留组件与配置的静态对应关系。需要这种类型保证的代码内装配优先使用 `ctx.use()`；配置文件和 Loader 入口需要额外验证输入。

### 从 TOML 加载

文件配置不会自动构造 dataclass。可以加一个接收 `object` 的入口，检查外部输入后再挂载类型化插件。在 `python/my_app/` 中创建空的 `__init__.py`，并保存以下 `plugins.py`：

```python
from cordis import plugin
from examples.tutorial import AppContext, GreeterConfig, greeter


@plugin
def file_greeter(ctx: AppContext, raw: object) -> None:
    if not isinstance(raw, dict):
        raise TypeError("greeter.config 必须是对象")
    prefix: object = raw.get("prefix")
    if not isinstance(prefix, str) or not prefix:
        raise ValueError("greeter.config.prefix 必须是非空字符串")
    ctx.use(greeter, GreeterConfig(prefix=prefix))
```

这里 `greeter` 是入口插件的子插件，会跟随入口一起卸载。为简化演示，接口和组件从 `examples.tutorial` 导入；正式项目将它们移到自己的共享模块。

在 `python/` 下保存 `plugins.toml`：

```toml
[[plugins]]
id = "greeter"
plugin = "my_app.plugins:file_greeter"
config = { prefix = "你好" }

[[plugins]]
id = "app"
plugin = "examples.tutorial:app"
```

把上一段 Loader 宿主中的 `for prefix ...` 循环替换为以下代码，其余入口与清理不变：

```python
await loader.load_file("plugins.toml")
for fiber in root.fibers:
    await fiber
    assert fiber.state is FiberState.ACTIVE
await root.parallel("greet", "Cordis")
```

宿主进程必须能导入定位符中的模块。TOML 使用 `[[plugins]]`；JSON 和 YAML 的顶层使用条目列表，YAML 还需安装当前项目的 `yaml` extra。

持续运行的宿主可在首次 `load_file()` 成功后注册配置监听：

```python
root.spawn(loader.watch_file("plugins.toml"), policy="cancel")
```

`watch_file()` 只在后续文件变更时重新读取配置；宿主仍需保持事件循环运行，并处理 `root.errors` 中的后台异常。

### 区分三种更新

| 需求 | 操作 |
| --- | --- |
| 改配置 | `fiber.update(config)`，或重新 `loader.reconcile(...)` / `load_file(...)` |
| 重新运行当前组件 | `fiber.restart()`；代码内传入组件的条目也可用 `loader.reload(id)` |
| 重新导入 Python 代码 | 字符串定位符条目用 `loader.reload(id)`，或 `loader.reload_changed([path])` |

HMR 根据静态导入关系决定受影响模块；它不能替换进程里所有被外部持有的旧对象。涉及 `externals` 边界时，`FullRestartRequired` 表示宿主需要重启进程。更详细的边界见[核心概念](core-concepts.md)。

## 7. 为两套实例隔离服务

下面的宿主把同名 `GREETER` 放到两个独立域中：

```python
import asyncio

from examples.tutorial import AppContext, GREETER, GreeterConfig, app, greeter


async def main() -> None:
    async with AppContext() as root:
        left = root.isolate(GREETER)
        right = root.isolate(GREETER)
        left_provider = left.use(greeter, GreeterConfig(prefix="A"))
        left_consumer = left.use(app)
        right_provider = right.use(greeter, GreeterConfig(prefix="B"))
        right_consumer = right.use(app)
        await root.flush()
        await asyncio.gather(
            left_provider, left_consumer, right_provider, right_consumer,
        )
        await root.parallel("greet", "Cordis", scope=left)
        await root.parallel("greet", "Cordis", scope=right)


asyncio.run(main())
```

问候分别为 `A Cordis` 和 `B Cordis`。同一组提供者与消费者必须复用同一个隔离 Context；两次 `root.isolate(GREETER)` 会创建两个不同的域。

服务隔离与事件范围分别设置。事件默认派发给匹配名称的全部监听器，所以这里显式传入 `scope`。派生 Context 共享运行时，不能用 `left.close()` 只关闭左侧；需要卸载对应 Fiber，或由单独的 Loader 管理这一组。

## 8. 检查和排错

```bash
uv run mypy
uv run python -m examples.tutorial
uv run python -m examples.graceful_unload
uv run python -m unittest discover -v
```

项目的 mypy strict 检查包含教程程序。你添加的业务文件还需要加入检查范围；也可以执行 `uv run mypy my_app`。

| 现象 | 优先检查 |
| --- | --- |
| 插件一直 `pending` | 注入服务是否存在、是否在同一个 Realm、父组件是否已激活 |
| `UndeclaredServiceError` | 消费插件是否声明了所需的 `inject`，是否导入了正确的共享键 |
| `MissingProvisionError` | 插件承诺的 `provide` 是否在初始化结束前全部注册 |
| `DuplicateServiceError` | 同一个 Realm 内是否启动了两个同名服务提供者 |
| 配置文件加载后没有预期效果 | 遍历 `root.fibers` 检查状态和错误，包括入口插件创建的子插件 |
| 卸载迟迟不结束 | 是否有未结束的 drain 任务、忽略取消的协程或阻塞的清理函数 |
| 描述符访问有补全但运行失败 | 静态类型通过不代表运行时依赖已就绪；仍需正确声明、装配与等待 |

`root.errors` 保存 `(异常, 所属 Fiber 或 None)` 的快照，`internal/error` 事件可接入日志。服务资源的卸载顺序和在途任务示例见 [`examples/graceful_unload.py`](../examples/graceful_unload.py)。
