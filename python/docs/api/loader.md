# 配置与加载器 API

[返回 API 索引](../api.md) · [插件与生命周期](plugins.md) · [代码热重载](hmr.md)

源码：[`loader.py`](../../src/cordis/loader.py)。Loader 接收带稳定 id 的目标插件清单，比较已有条目，执行增删、配置更新和服务隔离调整。

## Entry

```python
Entry(
    id,
    plugin=None,
    config=None,
    enabled=True,
    isolate={},
    intercept={},
    children=(),
)
```

不可变、带 slots 的配置数据对象。上面的 `{}` 表示默认空映射；源码使用工厂为每个条目创建独立容器。

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `id` | `str`，必填 | 当前 Loader 内的稳定条目名；同一清单不能重复 |
| `plugin` | `str \| PluginLike \| None` | 插件定义、回调、对象或 `package.module:attribute` 定位符 |
| `config` | `object` | 传入插件的配置，默认 `None`；不会自动转换为 dataclass |
| `enabled` | `bool` | 是否启用；默认 `True`，停用条目仍保留在 `entries` 中 |
| `isolate` | `Mapping[str, bool \| str] \| tuple[str, ...]` | 按字符串服务名配置隔离域，规则见下表 |
| `intercept` | `Mapping[str, object]` | 按字符串服务名给当前条目设置拦截元数据，读取后需收窄类型 |
| `children` | `tuple[Entry \| Mapping[str, Any], ...]` | 嵌套插件清单；默认为空 |

必须满足以下两种形式之一，否则抛出 `ValueError`：指定 `plugin`；或不指定 plugin、提供非空 `children`。不能同时指定插件和子条目。

Entry 会复制并冻结 isolate / intercept 的外层映射，把 children 转成 tuple；**不会深拷贝 config 或嵌套值**。更新时创建新配置，不要原地修改旧对象。

### isolate 的值

| 形式 | 结果 |
| --- | --- |
| `isolate=("store",)` | 等价于 `{"store": True}` |
| `{"store": True}` | 为当前条目分配独立且在协调更新间保持稳定的 Realm |
| `{"store": "tenant-a"}` | 同一 Loader 中，使用同一标签和服务名的条目共享 Realm |
| `{"store": False}` | 不增加本条目的隔离覆盖，沿父 Context 的规则解析 |

共享标签由当前 Loader 管理，不是跨 Loader 的全局命名空间。需要嵌套组内共享服务时，可以把隔离设置放在父组上，让子条目继承。

`Entry` 是兼容配置清单的动态容器，静态类型不会证明它的 plugin 与 config 匹配。代码内装配可使用 `component.configure(config)` 与 `loader.mount()`；外部数据先通过 `component.decode(raw, decoder)` 转换。Entry 不会自动调用 decoder。

## Loader

```text
Loader(ctx: ContextT)  # Loader[ContextT]
```

以只读的 `ctx` 作为装配入口，保留 Context 子类类型。Loader 自身不是异步上下文管理器；可在 `finally` 中 `await loader.close()`，或者由包围它的根 Context 统一关闭运行时。

### mount / unmount

```python
async def mount(
    entry_id: str,
    binding: BoundComponent[ConfigT, ContextT],
) -> Fiber[ConfigT, ContextT]: ...

async def unmount(entry_id: str) -> None: ...
```

先绑定配置，再将完整绑定交给 Loader：

```python
binding = worker.configure(WorkerConfig(interval=5))
fiber = await loader.mount("worker", binding)
await fiber.update(WorkerConfig(interval=10))
await loader.unmount("worker")
```

mount 等待当前启动转换完成；缺少依赖时可以返回处于 `PENDING` 的 Fiber，语义与 `await fiber` 相同。启动失败会清理本次挂载、释放 id 并抛出异常。id 与现有 mount 或 Entry 冲突时，在修改前抛出 `ValueError`。unmount 可移除任一入口的条目；不存在时抛出 `KeyError`。

mount 管理的条目不进入 `entries`，也不会被 `reconcile()` 的完整清单删除；`reconcile([])` 只清空声明式条目。两种入口共享 id 范围，清单与 mount 的 id 冲突会在修改前被拒绝。`close()` 清理两种入口的全部挂载。源码 HMR 仍只处理 Entry 的字符串定位符；类型化挂载通过 `reload(id)` 重启已有组件定义。

类型化挂载使用 Loader 的 Context；如需隔离或拦截，可先通过 `ctx.isolate()` / `ctx.intercept()` 创建装配 Context。完整示例见 [`typed_interfaces.py`](../../examples/typed_interfaces.py)。

### entries / get

```text
loader.entries -> tuple[Entry, ...]
loader.get(entry_id: str) -> FiberView[ContextT] | None
```

`entries` 返回这个 Loader 直接管理的声明式条目快照，包括停用项。`get()` 可以查询声明式条目及 mount 挂载，行为如下：

| 情况 | 结果 |
| --- | --- |
| 条目正在运行或等待依赖 | 对应 Fiber，可能是 ACTIVE、PENDING 或 FAILED |
| 已记录条目但没有挂载 Fiber，例如停用项 | `None` |
| 不存在该 id | 抛出 `KeyError`，不是返回 `None` |

嵌套组由内部 Loader 管理。父 Loader 的 `get()` 只能查询组的 id，不能用它直接查询子条目的 id。

字符串 id 不能恢复插件的具体配置类型，返回视图的 config 因此为 object，不暴露 update。ctx 保留 Loader 的 Context 子类类型。代码内需要更新配置时，保留 `ctx.use()` 或 `loader.mount()` 的返回值。

### reconcile

```python
async def reconcile(entries: Iterable[Entry | Mapping[str, Any]]) -> None: ...
```

应用**完整目标清单**，不是局部补丁。映射会通过 `Entry(**mapping)` 转成条目，未知字段和不合法组合会被拒绝。

| 与现状的差异 | 操作 |
| --- | --- |
| id 新增且 enabled 为真 | 导入、挂载插件 |
| 原来的 id 不在新清单中 | 卸载并移除条目 |
| enabled 改成假 | 卸载 Fiber，保留条目记录 |
| 停用项重新启用 | 重新挂载 |
| 同一 id 的 plugin 改变 | 卸载旧 Fiber，挂载新实现 |
| 插件相同，config 按 `!=` 比较发生变化 | 调用对应 `Fiber.update()` |
| isolate 改变 | 更新条目的 Realm，触发相关依赖重新判断 |
| intercept 改变 | 更新元数据，后续读取使用新适配结果；单独改变它不重启插件 |
| 没有相关变化 | 保留已有挂载 |

重复 id、非法 Entry 结构和启用候选的导入错误在修改前拒绝；组内条目的结构和导入也会预检。顶层新增或变更组件的 validate 在修改前运行，并记录规范化配置；激活时可能再次校验，因此校验器应可重复调用。

应用前保存旧 Entry、Component 和上下文设置。更新成功后才记录新配置；应用或启动失败时，清理本轮受影响挂载并恢复旧条目，未受影响条目保持原 Fiber。恢复包括被移除项、此前成功更新项，以及因提供者变化而失败的消费者和组内插件。恢复成功仍抛出原错误，恢复失败则聚合报告。外部副作用和自定义 hook 的原地修改不保证可撤销，也不提供变更期间的数据库式隔离。

父 Context 为 ACTIVE 时会等待本轮转换并传播新增启动失败；缺少依赖仍可返回 PENDING，父 Context 处于 LOADING 时子激活会延后。业务要求就绪时，仍应检查关键 Fiber：

```python
import asyncio
from dataclasses import dataclass

from cordis import Context, Entry, FiberState, Loader, plugin


@dataclass(frozen=True)
class Config:
    message: str


@plugin
def hello(_ctx: Context, config: Config) -> None:
    print(config.message)


async def main() -> None:
    async with Context() as root:
        loader = Loader(root)
        try:
            await loader.reconcile([Entry("hello", hello, Config("已加载"))])
            fiber = loader.get("hello")
            assert fiber is not None
            await fiber
            if fiber.state is not FiberState.ACTIVE:
                raise RuntimeError(f"插件未就绪：{fiber.state.value}")

            await loader.reconcile([Entry("hello", hello, Config("已更新"))])
        finally:
            await loader.close()


asyncio.run(main())
```

依次输出 `已加载`、`已更新`。同一 Loader 的 mount、unmount、reconcile、reload、HMR 和 close 共用锁。排队期间取消不会应用变更；操作开始后取消会等提交或恢复完成，再抛出 CancelledError，重复取消也不会遗弃收尾。成功提交后收到取消不代表变更被撤销，可用 get/entries 查询结果。不同 Loader 的操作由宿主协调。

管理操作必须能等待目标 Fiber；受管任务卸载自身或依赖、初始化期间递归修改同一 Loader，会在等待前抛出 InactiveContextError，避免自锁。更新失败后 Entry 保留旧配置，再次提交相同失败配置仍会重试。

### reload

```python
async def reload(entry_id: str) -> FiberView[ContextT]: ...
```

重新加载指定条目，返回当前 Fiber。未知 id 抛出 `KeyError`。

- plugin 是对象或 Component：通常调用同一个 Fiber 的 `restart()`，不会重新执行 Python 模块导入。
- plugin 是字符串定位符：调用 HMR 重导入入口模块，替换 Fiber；失败时尝试恢复，见 [HMR](hmr.md)。
- 通过 mount 挂载：重启同一个 Fiber，使用其当前配置，包括通过类型化 Fiber.update() 成功更新的配置。

它面向需要重新运行的条目，不能替代 enabled 配置：当前对象插件路径会在 Fiber 缺失时直接启动条目，而字符串路径会因停用项没有启动而报错。启停统一通过 `reconcile()` 管理。

直接 reload 入口模块不等同于追踪共享模块变更；如果修改的是被导入的辅助模块，使用 `reload_changed()`。

### reload_changed

```python
async def reload_changed(
    changed: Iterable[str | Path],
    *,
    externals: Iterable[str] = (),
) -> tuple[str, ...]: ...
```

根据文件路径或已加载模块名查找受影响的插件，返回本次处理的条目 id 元组；无匹配时返回 `()`。是 `HotReloader(loader, externals=...).reload_changed(changed)` 的便捷入口，参数与限制见[热重载参考](hmr.md#reload_changed)。

### close

```python
async def close() -> None: ...
```

卸载当前 Loader 的所有挂载，清空条目记录。会尝试清理每一项，并把多个普通异常汇总为 `ExceptionGroup`。

它不关闭整个 Context，也不自动停止在宿主中启动的文件监听任务；请将监听任务交给 Context 管理，并按宿主的退出流程停止它们。

## 配置文件

### load_file / load_json

```python
async def load_file(path: str | Path) -> None: ...
```

以 UTF-8 读取文件，根据扩展名解析，再调用 `reconcile()`。`load_json` 是 `load_file` 的别名，仍根据扩展名选择格式，不局限于 JSON。

| 扩展名 | 结构 |
| --- | --- |
| `.json` | 顶层为条目数组 |
| `.toml` | 使用 `[[plugins]]` 数组表 |
| `.yaml` / `.yml` | 顶层为条目列表，需要安装可选 YAML 依赖 |

不支持的扩展名抛出 `ValueError`，解析结果不是列表时抛出 `TypeError`；文件读取、语法解析、导入和插件更新异常按各自路径传播。没有安装 YAML 依赖却加载 YAML，会抛出 `RuntimeError`。

TOML 的条目形式如下，假设项目已经定义了 `my_app.plugins:server`，并且该插件接收含 port 的字典配置：

```toml
[[plugins]]
id = "server"
plugin = "my_app.plugins:server"
enabled = true

[plugins.config]
port = 8080
```

这类文件使用 Entry 字段；它与 CSV 示例的 `[database]`、`[validation]` 业务格式不同。后者由应用先解析成 dataclass，再调用 reconcile。完整文件配置演练见[教程](../tutorial.md#从-toml-加载)。

### watch_file

```python
async def watch_file(
    path: str | Path,
    *,
    interval: float = 0.5,
    stop: asyncio.Event | None = None,
) -> None: ...
```

长期轮询文件的 `st_mtime_ns`。检测到变化后调用 `load_file()`；设置 stop 或取消任务后结束。初次调用只记录文件状态，**不会执行首次加载**。

在已创建的 root / loader 中，先加载再监听：

```python
await loader.load_file("cordis.toml")
watcher = root.spawn(
    loader.watch_file("cordis.toml", interval=0.5),
    policy="cancel",
    name="cordis:config",
)
```

路径必须存在。`interval` 是检查间隔，调用方应传有限正数；stop 在循环边界检查，不保证设置后立即返回。文件错误、配置解析或更新错误会结束 watcher，不会自动重试。需要“修正配置后继续监听”的业务策略，参考 [CSV 宿主](../scenarios.md#配置如何选择提供方)。

## group

`group` 是导出的 **Component**，不是装饰器或普通函数：

```text
Component[Iterable[Entry | Mapping[str, Any]], Context]
```

它为 children 创建内部 Loader，原地更新时协调子清单，退出时卸载子项。通常通过 Entry 的 children 隐式使用：

```python
parent = Entry("workers", children=(
    Entry("email", "my_app.plugins:email_worker"),
    Entry("index", "my_app.plugins:index_worker"),
))
```

也可以显式 `ctx.use(group, entries)`，再通过返回的 Fiber 更新子清单。每个组有自己的条目 id 范围；子插件只能在父组激活后启动。

## import_plugin

```text
import_plugin(locator: str) -> PluginLike
```

同步导入模块并解析属性，例如 `my_app.plugins:server`；冒号后可以是多级属性路径。缺少模块名、冒号或属性名会抛出 `ValueError`，模块和属性查找异常直接传播。

返回被定位的对象，**不挂载插件**；是否为合法插件由后续 `ctx.use()` / Loader 挂载检查。字符串导入是动态边界，不能据此得到具体配置类型的静态保证。
