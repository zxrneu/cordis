# 迁移到类型化运行接口

本页对应当前仓库的 Python 实现，说明 FiberView、事件键和配置绑定的迁移方式。先安装当前源码，再运行 `uv run mypy` 检查业务代码；版本号相同不代表已安装的包包含这些仓库改动。

## 先确定调用的用途

| 原有用法或需求 | 现在使用 | 得到的保证 |
| --- | --- | --- |
| 从 `ctx.fiber`、运行时列表、错误记录或 `loader.get(id)` 查看插件 | `FiberView[ContextT]` | 读取状态、等待、重启和卸载；不会宣称可接受任意配置 |
| 更新某个已知插件的配置 | 保存 `ctx.use()` 或 `await loader.mount()` 返回的 `Fiber[ConfigT, ContextT]` | `update()` 检查 ConfigT |
| 业务通知 | 共享 `EventKey[Payload]`，使用 on/once、emit/parallel | 检查通知与回调的参数类型 |
| 单个处理器提供查询结果 | 共享 `QueryKey[Request, Result]`，使用 handle/query | 查询返回 Result 或 None |
| 在代码中把插件交给 Loader | `component.configure(config)` → `loader.mount(id, binding)` | 插件、配置、Context 的类型关系保持到挂载完成 |
| JSON/TOML 等外部配置 | `await component.decode(raw, decoder)` → mount | decoder 将 object 转成 ConfigT 后才绑定 |
| 动态插件清单、分组或字符串定位符 HMR | 继续使用 Entry/reconcile/load_file | 保留动态装配；输入校验仍由边界代码负责 |

## 1. 保存能更新配置的 Fiber

下面是独立的可运行片段，在 `python/` 目录执行，复用仓库示例中的插件：

```python
import asyncio

from cordis import Loader
from examples.typed_interfaces import AppContext, WorkerConfig, worker


async def main() -> None:
    async with AppContext() as root:
        loader = Loader(root)
        fiber = await loader.mount("worker", worker.configure(WorkerConfig("hello")))
        view = loader.get("worker")
        assert view is not None
        print(view.state.value)
        await fiber.update(WorkerConfig("updated"))
        await loader.unmount("worker")


asyncio.run(main())
```

`fiber` 保留 WorkerConfig，`view` 的配置未知，因此 `view.update(...)` 会产生类型错误。`ctx.fiber`、`ctx.fibers`、`ctx.errors` 中的归属以及 `loader.reload(id)` 也返回视图。`await view` 和 `await view.wait()` 仍返回视图，不能借等待恢复配置写入能力。

FiberView 的属性只读，可调用 dispose、restart、wait；它不暴露 Component.apply 等需要配置类型的操作。`Fiber.ctx` 和 `Loader.ctx` 也只读，需要别的装配 Context 时创建新的 Loader。视图是静态协议，运行时仍指向原 Fiber，不深度冻结配置对象。

## 2. 把通知和查询分别迁移

把键定义在共享契约模块，发送方和接收方导入同一个对象：

```python
from cordis import EventKey, QueryKey

MESSAGE = EventKey[str]("message")
FIND_USER = QueryKey[int, str]("find_user")
```

下面的函数在活跃 Context 上注册并调用这两个契约：

```python
from cordis import Context


async def use_events(ctx: Context) -> None:
    def receive(message: str) -> None:
        print(message)

    async def find_user(user_id: int) -> str | None:
        return "Cordis" if user_id == 1 else None

    ctx.on(MESSAGE, receive)
    ctx.emit(MESSAGE, "hello")
    await ctx.parallel(MESSAGE, "wait for listeners")
    ctx.handle(FIND_USER, find_user)
    user = await ctx.query(FIND_USER, 1)
    if user is not None:
        print(user.upper())
```

通知有一个 payload 参数，多个字段用 dataclass 打包；无数据通知可用 EventKey[None] 并传 None。回调应返回 None 或 Awaitable[None]。原来回调有业务结果时，按调用目的选类型化服务或 QueryKey。

QueryKey 每个运行时只允许一个处理器，重复 handle 会抛出 DuplicateQueryError。无有效处理器或处理器未找到结果时返回 None；False 是有效结果。因此 query 不能直接替代多监听器短路链：原有 serial/bail 会跳过 None 和 False，两者语义不同。waterfall 仍是动态的中间件接口。

**EventKey 和 QueryKey 按对象身份匹配，名称仅用于诊断。** 重新构造同名键会得到另一个通道，也不与字符串事件互通。迁移一条事件时要同时替换注册方和发送方；ServiceKey 继续采用字符串名字匹配。源码热重载时也要保持契约模块稳定，或让参与通信的模块共同采用新的键。

异步通知和查询任务归属注册方 Fiber / Effect，支持 task_policy 和卸载 timeout。处理器在 LOADING 或 ACTIVE 且注册作用域开放时可执行；卸载先处理在途任务再释放资源。查询不建立服务依赖，也没有 Realm 或 scope 路由；需要依赖顺序时仍声明 inject。具体签名见[事件 API](api/context.md#事件)。

## 3. 将外部配置转换放在绑定之前

`configure()` 只绑定已有 ConfigT，不执行插件初始化或 validate。原始数据需要解码时，使用返回 ConfigT 的函数；在活跃 Loader 内可以这样调用：

```python
from cordis import Loader
from examples.typed_interfaces import AppContext, decode_config, worker


async def mount_external(loader: Loader[AppContext], raw: object) -> None:
    binding = await worker.decode(raw, decode_config)
    await loader.mount("external-worker", binding)
```

decoder 可同步或异步，每次 decode 执行一次；错误在挂载前抛出。插件的 validate 继续接收已转换的 ConfigT，按激活、更新的原有规则执行。`decode()` 不会自动替换正在运行的同名条目；已有类型化 Fiber 更新时可先 `config = decode_config(raw)`，再 `await fiber.update(config)`。

## 4. Loader 两种入口的管理范围

| 操作 | mount 条目 | Entry/reconcile 条目 |
| --- | --- | --- |
| 类型化写配置 | 保存 mount 返回的 Fiber | 改目标 Entry 后 reconcile |
| entries | 不包含 | 包含，含停用项 |
| get(id) | 返回 FiberView | 返回 FiberView，停用项可能为 None |
| reconcile([]) | 保留 | 卸载并移除 |
| unmount(id) / close() | 可以清理 | 可以清理 |
| reload(id) | 用最新配置重启同一个组件定义 | 对象定义重启；字符串定位符执行源码 HMR |
| reload_changed(paths) | 不参与源码 HMR | 根据字符串定位符和静态导入关系匹配 |

两种入口共享 id 范围，冲突在修改前被拒绝；查不到 id 时 get/unmount/reload 抛出 KeyError。mount 会等待当前启动转换并传播初始化错误，失败后清理本次挂载；缺少依赖时仍可能返回 PENDING。宿主应串行协调 mount、unmount、reconcile、reload 和 close。

## 验证迁移

```bash
cd python
uv run mypy
uv run python -m examples.typed_interfaces
uv run python -m unittest tests.test_typed_interfaces tests.test_typing -v
```

完整源码见[类型化接口示例](../examples/typed_interfaces.py)，更多类型推断与动态边界见[类型指南](typing.md)。[审计记录](typing-any-audit-2026-09-11.md)保留了修复前的证据；其中标明的历史签名不代表当前接口。
