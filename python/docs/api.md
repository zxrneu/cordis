# Python Cordis API 参考

这份文档对应本仓库 Python 实现，当前包版本为 **0.2.0**，要求 **Python 3.11+**。它说明公开接口的参数、返回值和运行时行为。第一次接触项目，请先读[入门指南](zh-CN.md)；需要从头编写插件，读[使用教程](tutorial.md)。

公开对象统一从 `cordis` 导入，范围以 [`cordis.__all__`](../src/cordis/__init__.py) 为准。这里没有把上游 TypeScript API 当作 Python 已实现的功能。

## 按任务查找

| 你要做什么 | API | 参考页 |
| --- | --- | --- |
| 定义插件、声明依赖和配置校验 | `plugin` / `component`、`Component`、`Plugin` | [插件与生命周期](api/plugins.md#定义插件) |
| 挂载插件、更新配置、等待和卸载 | `Context.use()`、`Fiber`、`FiberState` | [挂载与运行句柄](api/plugins.md#挂载插件) |
| 定义带类型的服务接口 | `ServiceKey[T]`、Context 描述符 | [服务键](api/context.md#servicekey) |
| 注册、读取和替换服务 | `provide()`、`service()`、`get()`、`set()`、`notify()` | [服务操作](api/context.md#服务操作) |
| 隔离服务、按调用方适配服务 | `isolate()`、`intercept()`、`metadata()` | [隔离与拦截](api/context.md#隔离与拦截) |
| 登记清理操作、管理后台任务 | `effect()`、`EffectHandle`、`spawn()`、`TaskPolicy` | [资源与任务](api/context.md#资源与任务) |
| 注册监听器、派发事件 | `on()`、`once()`、`emit()`、`parallel()`、`serial()`、`bail()`、`waterfall()` | [事件](api/context.md#事件) |
| 装配插件树、加载或监听配置文件 | `Entry`、`Loader`、`group`、`import_plugin()` | [配置与加载器](api/loader.md) |
| 重载修改后的 Python 模块 | `HotReloader`、`ModuleGraph`、`FullRestartRequired` | [代码热重载](api/hmr.md) |
| 查看错误和生命周期状态 | `Context.errors`、`Context.fibers`、内部事件 | [观测与异常](#观测与异常) |

## 最小完整示例

下面这份程序声明一个服务，挂载提供方和调用方，然后更新提供方的配置。安装当前源码后，把代码保存成文件即可运行：

```python
import asyncio
from dataclasses import dataclass

from cordis import Context, FiberState, ServiceKey, plugin


TITLE = ServiceKey[str]("title")


class AppContext(Context):
    title = TITLE


@dataclass(frozen=True)
class Config:
    title: str


@plugin(provide=[TITLE])
def provider(ctx: AppContext, config: Config) -> None:
    ctx.provide(TITLE, config.title)


@plugin(inject=[TITLE])
def consumer(ctx: AppContext, _config: None) -> None:
    print(ctx.title)


async def main() -> None:
    async with AppContext() as root:
        reader = root.use(consumer)
        writer = root.use(provider, Config("你好，Cordis"))
        await root.flush()
        for fiber in (writer, reader):
            await fiber
            assert fiber.state is FiberState.ACTIVE

        await writer.update(Config("配置已更新"))
        await root.flush()
        await reader
        assert reader.state is FiberState.ACTIVE


asyncio.run(main())
```

输出：

```text
你好，Cordis
配置已更新
```

`provider` 是可复用的 **Component**，`writer` 是本次挂载的 **Fiber**，`root.title` 是提供方注册的**服务值**。配置更新触发提供方重启，依赖它的调用方也随之重新激活。

## 阅读签名时的约定

参考页合并展示部分重载，以便阅读；精确的静态类型重载见对应源码。代码块中的签名是接口说明；写明“完整示例”的代码块可以独立运行，其余示例会说明依赖的上下文。

| 记号 | 含义 |
| --- | --- |
| `T`、`D` | 服务值、任务结果或默认值的类型 |
| `ConfigT` | 某个插件的配置类型；无配置用 `None` |
| `ContextT` / `Self` | 当前 Context 子类，派生操作保留它的类型 |
| `ServiceName` | `str` 或 `ServiceKey[T]`，用于异构服务声明时擦除 `T` |
| `Injection` | 服务名的可迭代集合，或 `{服务名: 拦截元数据}` 映射 |
| `EventScope` | `Context`、`Callable[[Context], bool]` 或 `None` |
| `PluginLike` | 本文对 Component、插件回调、具有 `apply()` 的对象的统称；Loader 还可接受字符串定位符 |

`ServiceName`、`Injection`、`EventScope`、`PluginLike` 是这里用于解释签名的记号，**不是从 `cordis` 顶层导出的名称**。需要导入的公开类型别名见[资源类型](api/context.md#effectcallbackeffectresult-与-cleanup)。

主要异步操作需要运行中的 asyncio 事件循环。`use()`、`provide()`、`on()` 和 `spawn()` 本身是同步调用；带 `async` 的方法才需要 `await`。`use()` 返回的 Fiber 也可以直接 `await`。

## 观测与异常

### 运行状态

所有 Context 派生视图共享同一个运行时：

| 属性或事件 | 内容 |
| --- | --- |
| `ctx.fiber` | 当前 Context 所属的 Fiber；根 Context 也有根 Fiber |
| `ctx.fibers` | 运行时中尚未移除的普通 Fiber 快照，不包含根 Fiber；不限于当前 Context 的子插件 |
| `ctx.errors` | `tuple[tuple[Exception, Fiber \| None], ...]` 错误历史快照；读取不会清空 |
| `internal/status` | 回调参数 `(fiber, previous_state)`；新状态读取 `fiber.state` |
| `internal/error` | 回调参数 `(error, fiber)`；无法归属时 `fiber` 可以是 `None` |

在根 Context 创建后、挂载业务插件前注册监听器，例如：

```python
root.on("internal/status", lambda fiber, old: print(old, "->", fiber.state))
root.on("internal/error", lambda error, fiber: print(fiber, error))
```

插件初始化失败通常保存在 `fiber.error` 中，`await fiber` 会重新抛出。`ctx.flush()` 只等待生命周期转换结束，不会把所有失败转换成调用异常，也不等待后台任务完成。读取关键 Fiber 的状态仍然必要。

### 公开异常

下列异常均可从 `cordis` 导入：

| 异常 | 基类 | 常见触发条件 |
| --- | --- | --- |
| `CordisError` | `RuntimeError` | 核心运行时错误的基类 |
| `InactiveContextError` | `CordisError` | 在已关闭或过期的作用域创建资源；重启已卸载的 Fiber；受管任务等待自身或依赖的卸载 |
| `InactiveServiceError` | `CordisError` | 严格读取已声明、但当前依赖视图中不可用的服务 |
| `UndeclaredServiceError` | `CordisError` | 严格读取未声明的服务；根 Context 严格读取不存在的服务也会抛出它 |
| `DuplicateServiceError` | `CordisError` | 同一 Realm 重复提供服务，或 `set()` 修改自己不拥有的服务 |
| `MissingProvisionError` | `CordisError` | 插件初始化完成，却没有注册 `provide=[...]` 声明的服务 |
| `FullRestartRequired` | `RuntimeError` | HMR 的受影响模块触及配置的外部边界；它不是 `CordisError` 的子类 |

另外，参数错误会使用 `TypeError` / `ValueError`，缺失的 Loader 条目使用 `KeyError`；用户回调、文件解析及模块导入的异常也可能直接传播。多个清理操作失败、并发事件失败、部分 HMR 失败会以 Python `ExceptionGroup` 汇总。

这些操作不会自动停止整个应用。需要终止进程、重试或记录告警时，由宿主处理异常；详细恢复范围见[实现与维护参考](implementation.md#当前限制与风险)。

## 公开名称索引

| 类别 | 顶层导出 |
| --- | --- |
| 插件 | [`plugin` / `component`、`Component`、`Plugin`](api/plugins.md#定义插件) |
| 生命周期 | [`Fiber`、`FiberState`](api/plugins.md#fiber) |
| 上下文与服务 | [`Context`、`ServiceKey`](api/context.md) |
| 资源与任务 | [`EffectHandle`、`EffectCallback`、`EffectResult`、`Cleanup`、`TaskPolicy`](api/context.md#资源与任务) |
| 配置 | [`Entry`、`Loader`、`group`、`import_plugin`](api/loader.md) |
| 热重载 | [`HotReloader`、`ModuleGraph`](api/hmr.md) |
| 异常 | [`CordisError`、`InactiveContextError`、`InactiveServiceError`、`UndeclaredServiceError`、`DuplicateServiceError`、`MissingProvisionError`、`FullRestartRequired`](#公开异常) |
