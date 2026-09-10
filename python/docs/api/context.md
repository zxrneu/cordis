# Context、服务与任务 API

[返回 API 索引](../api.md) · [插件与生命周期](plugins.md) · [Loader](loader.md)

源码：[`core.py`](../../src/cordis/core.py)、[`keys.py`](../../src/cordis/keys.py)。签名中的通用类型记号见 [API 阅读约定](../api.md#阅读签名时的约定)。

## Context

```python
Context()
```

创建独立运行时和一个处于 `ACTIVE` 状态的根 Fiber。通常在 asyncio 入口中使用：

```python
async with Context() as root:
    # 在这里挂载插件、注册服务或监听器。
    pass
```

`__aenter__()` 返回自身，退出时调用 `close()`。继承 Context 可以添加服务描述符；通过 `isolate()`、`intercept()` 或插件挂载创建的派生 Context 会保留子类类型，但不会再次调用子类的 `__init__()`。

| 方法 / 属性 | 返回值 | 作用 |
| --- | --- | --- |
| `use(value, config=None)` / `plugin(...)` | `Fiber[ConfigT, ContextT]` | [挂载插件](plugins.md#contextuse--contextplugin) |
| `inject(dependencies, callback, config=None)` | `Fiber[ConfigT, ContextT]` | [挂载有依赖的临时插件](plugins.md#contextinject) |
| `fiber` | `Fiber[Any, Any]` | 当前所属 Fiber |
| `fibers` | `tuple[Fiber[Any, Any], ...]` | 整个运行时的普通 Fiber 快照，不含根 Fiber |
| `errors` | `tuple[tuple[Exception, Fiber[Any, Any] \| None], ...]` | 整个运行时的错误历史快照 |
| `await flush()` | `None` | 等待已安排的生命周期转换稳定；不等待后台任务或未来依赖 |
| `await close()` | `None` | 关闭整个运行时，包括所有插件、受管任务和根资源 |

**派生 Context 的 `close()` 也会关闭整个运行时。** 只想卸载某一部分时，使用 `Fiber.dispose()` 或 `EffectHandle`。

多次调用 `close()` 会等待同一个关闭任务，不会重复清理；关闭失败时会再次看到该任务的异常。取消某个关闭等待者不会取消已经开始的关闭流程。资源清理可以抛出 `ExceptionGroup`。

## ServiceKey

```text
ServiceKey[T](name: str)
```

不可变的带类型服务键。`name` 必须是非空字符串，否则抛出 `ValueError`。`T` 是不变类型参数，用来检查服务注册值、读取值和适配器的类型；它不会在运行时对服务做 `isinstance` 检查。

```python
from typing import Protocol

from cordis import Context, ServiceKey


class Clock(Protocol):
    def now(self) -> float: ...


CLOCK = ServiceKey[Clock]("clock")


class AppContext(Context):
    clock = CLOCK
```

`AppContext.clock` 的类型是 `ServiceKey[Clock]`；`ctx.clock` 的类型是 `Clock`，等价于 `ctx.service(CLOCK)`。描述符只读，直接赋值会抛出 `AttributeError`；替换已注册服务使用 `ctx.set()`。

键名与字符串服务名共享命名空间，`CLOCK` 和 `"clock"` 指向同名服务。把键和接口放在共享定义模块里，避免同名键宣称不同的值类型。服务键本身不创建服务，也不自动声明依赖。

## 服务操作

### provide

以下为使用服务键时的签名；字符串名字也可使用，但会丢失值类型的关联：

```python
def provide(
    name: ServiceKey[T],
    value: T,
    *,
    check: Callable[[Context], bool] | None = None,
    adapt: Callable[[T, object, Context], T] | None = None,
) -> EffectHandle: ...
```

在当前服务 Realm 中注册对象。返回清理句柄，插件退出时自动撤销注册，也可以提前 `await handle()` 移除服务。同一 Realm 已有提供者时抛出 `DuplicateServiceError`。

| 参数 | 作用 |
| --- | --- |
| `name` / `value` | 服务键和匹配的对象或值 |
| `check` | 同步可用性判断，接收正在解析服务的消费者 Context；返回假值时视为不可用 |
| `adapt` | 每次读取时调用 `(value, metadata, consumer_ctx)`，返回符合原接口的适配值 |

提供方初始化时即可调用 `provide()`，但只有它进入 `ACTIVE` 后，新消费者才可获得该服务。退出中的消费者通过已经建立的依赖视图读取服务，直到其清理结束。

`check` 抛出异常时会记录运行时错误，并将这次解析视为不可用。外部条件变化后要调用 `notify()`，它不是自动轮询的谓词。`adapt` 的结果不缓存；异常会传播到读取方。

仅撤销服务注册不会自动调用服务对象的 `close()`。对象资源应另行登记清理，例如生成器插件先 `yield connection.close`，再 `ctx.provide(...)`。完整资源模式见[插件初始化返回值](plugins.md#初始化可以返回什么)。

### service、下标和属性读取

```text
ctx.service(key: ServiceKey[T]) -> T
ctx[key] -> T
ctx.service(name: str) -> Any
ctx[name] -> Any
```

严格读取已声明的服务。Context 子类上的服务描述符也走同一条路径；动态的 `ctx.some_name` 仅在不存在同名普通属性时按字符串服务名解析。

- 插件必须通过 `inject` 声明依赖，或沿同一 Realm 的父 Context 继承可用的依赖视图。
- 已声明但不可用，抛出 `InactiveServiceError`；未声明则抛出 `UndeclaredServiceError`。
- 根 Context 可以直接读取可用服务，无需声明 inject；不存在时仍会抛出 `UndeclaredServiceError`。

依赖视图固定的是提供者关系，不是服务值的深拷贝。不要跨插件生命周期缓存旧服务对象，退出后应重新获取当前服务。

### get

```text
ctx.get(key: ServiceKey[T], *, include_inactive=False) -> T | None
ctx.get(key: ServiceKey[T], default: D, *, include_inactive=False) -> T | D
ctx.get(name: str, default=None, *, include_inactive=False) -> Any
```

反射式查询：找不到可用服务时返回 default，不检查当前插件是否声明了依赖。仍按当前 Realm 解析，并执行服务的 `check` 和适配逻辑。

`include_inactive=True` 放宽提供者必须处于 `ACTIVE` 的限制，**不会**跳过 `check`，也不会恢复已经移除的服务。它适合诊断，不能保证获得的对象可用于业务调用。

`get()` 不会为插件建立新的生命周期依赖，也不使用严格读取的固定依赖视图。需要在任务和清理期间持续使用的服务，应声明 `inject` 并严格读取。

### set

```text
ctx.set(key: ServiceKey[T], value: T) -> None
ctx.set(name: str, value: object) -> None
```

替换当前 Fiber **自己提供**的服务值，保留服务注册及 `check` / `adapt`。服务不存在或属于别的 Fiber 时，抛出 `DuplicateServiceError`。

它不会自动清理旧对象、通知可用性变化或重启消费者；后续读取会使用新值，已经缓存的旧值不会被替换。资源型服务需要由提供方安排旧对象的生命周期。

### notify

```text
ctx.notify(*names: ServiceName) -> None
```

通知运行时重新判断这些服务在当前 Realm 中的依赖条件。典型用途是 `check` 依赖的就绪标记发生变化；调用后可 `await ctx.flush()` 等待引起的生命周期转换。

`notify()` 不是强制重启广播：如果提供者身份和可用性没有改变，消费者不会重启。

## 隔离与拦截

### isolate

```text
ctx.isolate(*names: ServiceName, realm: object | None = None) -> Self
```

返回新的 Context 视图，为指定服务分配独立 Realm，不修改原 Context。其他服务继续沿父级规则解析。

```python
left = root.isolate(CLOCK)
right = root.isolate(CLOCK)
# 在 left / right 分别挂载 CLOCK 的提供者与消费者，即可各自使用自己的服务。
```

传入 `realm` 可以让多个派生视图共享一个服务域。显式 realm 只允许指定一个服务，否则抛出 `ValueError`。建议创建并复用 `shared = object()` 这样的不透明标识；当前实现对假值 realm 会新建标识。不要把同一个标识复用于不同服务名，因为底层服务注册以 Realm 为键。

隔离控制的是服务解析，不等同于进程沙箱，也不会自动隔离事件。

### intercept / metadata

```text
ctx.intercept(name: ServiceName, metadata: object) -> Self
ctx.metadata(name: ServiceName) -> object
```

`intercept()` 返回添加了服务元数据的派生 Context，`metadata()` 读取合并后的元数据。它不会直接替换服务或自动重启插件。

| 元数据层 | 合并规则 |
| --- | --- |
| 没有配置 | 返回 `None` |
| 每层都是 Mapping | 从外向内浅合并，内层覆盖同名键 |
| 任一层不是 Mapping | 返回最内层的值 |

`@plugin(inject={KEY: metadata})` 会为该消费者添加一层元数据。提供方可以通过 `provide(adapt=...)` 解释它；没有显式 adapt 时，运行时也会尝试调用服务对象的 `__cordis_intercept__(metadata, consumer_ctx)`。两者都没有时，读取原服务值。

## 资源与任务

### EffectCallback、EffectResult 与 Cleanup

这三个类型别名均可从 `cordis` 导入：

```python
Cleanup = Callable[[], object]
EffectCallback = Callable[[], EffectResult]
```

`Cleanup` 是无参数清理函数，返回值若可等待，运行时会等待它；其他返回值被忽略。不要提前调用它再登记，应该传函数本身。

`EffectResult` 接受以下值，或产生这些值的 awaitable：

- `None`、`Cleanup` 或 `EffectHandle`；
- 逐项产生 `Cleanup | EffectHandle | None` 的同步或异步可迭代对象；
- 同步或异步上下文管理器。

它描述的是**初始化操作与逆操作**，不用于承载任意业务结果。字符串、字节串和映射不会被当作清理列表处理。业务结果使用服务方法、事件返回值或 `spawn()` 返回的 Task。

### effect

```python
async def effect(
    callback: EffectCallback,
    label: str = "anonymous",
) -> EffectHandle: ...
```

执行无参数 callback，收集它创建的资源和返回的清理操作；等待初始化完成后返回清理句柄。`label` 用于诊断，不是唯一标识。

回调中的 `ctx.on()`、`ctx.provide()`、`ctx.use()`、`ctx.spawn()` 会归入当前 Effect。初始化失败会尝试撤销已经登记的操作并抛出异常；撤销也失败时会汇总为 `ExceptionGroup`。

下面是可以独立运行的完整示例，手动释放 Effect 后监听器不再响应：

```python
import asyncio

from cordis import Cleanup, Context


async def main() -> None:
    async with Context() as root:
        seen: list[int] = []

        def attach() -> Cleanup:
            root.on("item", seen.append)
            return lambda: print("资源已释放")

        release = await root.effect(attach, label="subscription")
        root.emit("item", 1)
        await release()
        root.emit("item", 2)
        assert seen == [1]


asyncio.run(main())
```

### EffectHandle

`provide()`、`on()`、`once()` 和 `await effect()` 返回的可调用清理对象：

```python
await handle()  # 正确：调用后等待清理
```

不是 `await handle`，也不是同步执行 `handle()` 后丢弃返回值。清理具有幂等性；已经开始的清理由重复调用共同等待，记录的清理错误仍可能再次抛出。

清理整个嵌套 Effect 会处理其中的任务、子插件和资源。`on()` 返回的句柄只移除监听器，不会取消此前已经触发的回调任务；这些任务仍由注册监听器时的作用域管理。

### spawn / TaskPolicy

```python
def spawn(
    awaitable: Awaitable[T],
    *,
    policy: TaskPolicy = "drain",
    timeout: float | None = None,
    name: str | None = None,
) -> asyncio.Task[T]: ...
```

在当前 Fiber 或嵌套 Effect 中启动受管异步任务，返回普通 asyncio Task，保留结果类型。`TaskPolicy` 是公开别名 `Literal["drain", "cancel"]`。

| 参数 | 行为 |
| --- | --- |
| `awaitable` | 要执行的 coroutine、Future 或其他可等待对象；不是普通函数 |
| `policy="drain"` | 卸载时停止接收新任务，等待当前任务自然结束 |
| `policy="cancel"` | 卸载时立即请求取消任务，并等待它退出 |
| `timeout=None` | drain 默认没有期限；指定时须为有限、非负秒数 |
| `name=None` | Task 名称；默认包含所属 Fiber 的名称 |

`timeout` 从卸载开始计算，只影响 drain 的宽限期；超时后请求取消，并继续等待协程退出。它不是单次业务操作的执行超时，`cancel` 策略也不会因此等待一个宽限期。

在运行中的 Context 内可以这样取得有类型的结果：

```python
task = ctx.spawn(asyncio.sleep(0, result=42), policy="drain", timeout=5)
result = await task  # int
```

任务失败时，等待它会抛出原异常，同时运行时会记录所属 Fiber 并触发 `internal/error`；不会自动把已激活 Fiber 改成 `FAILED`。普通取消不作为运行时错误记录。

所有任务先结束，再按 LIFO 清理资源；提供者等待依赖它的消费者完成退出。取消需要协程响应，不能中断同步阻塞调用或强制终止线程。业务调用需要独立的执行超时时，应在协程内使用 `asyncio.timeout()`。

裸 `asyncio.create_task()` 不会自动纳管。不要从受管任务中等待自己的 Fiber、父插件、依赖服务或整个运行时关闭；把该请求交给宿主。

## 事件

事件名是字符串，参数与结果属于动态接口，没有 `ServiceKey[T]` 那样的静态关联。

### on / once

```python
def on(
    name: str,
    callback: Callable[..., object],
    *,
    prepend: bool = False,
    global_: bool = False,
    task_policy: TaskPolicy = "drain",
    timeout: float | None = None,
) -> EffectHandle: ...
```

`once()` 接受相同参数与默认值，最多实际执行一次 callback，执行前移除监听器。

| 参数 | 含义 |
| --- | --- |
| `name` | 事件名称 |
| `callback` | 接收派发时的 `*args`，不会额外注入 Context |
| `prepend` | `True` 时放到监听器列表前面；默认追加 |
| `global_` | 绕过派发时的 scope 过滤；仍遵守所属作用域的生命周期 |
| `task_policy` / `timeout` | callback 返回 awaitable 时使用的任务退出策略，含义同 `spawn()` |

监听器在注册它的 Fiber / Effect 中执行，所创建的资源归属注册方；插件开始退出后不再接收新事件。异步回调会自动转为受管任务。若同步回调返回该 Fiber 已有的受管 Task，则复用其原策略。

当前 `once()` 使用异步包装：即使用户 callback 是同步函数，也应通过 `emit()`、`parallel()` 或 `serial()` 等支持 awaitable 的方式触发；不能与同步限定的 `bail()` 搭配。使用 `emit()` 时，once 的副作用不保证在 emit 返回前发生。

### 派发方式速查

各方法都接受 `name: str`、`*args: object` 和仅关键字参数 `scope=None`：

| 方法 | 返回值 | 执行方式与异常 |
| --- | --- | --- |
| `emit(name, *args, scope=None)` | `None` | 同步调用普通监听器，安排异步回调但不等待。回调错误记入运行时，继续派发 |
| `await parallel(name, *args, scope=None)` | `None` | 并发等待所有监听器；不返回结果列表。普通异常汇总为 `ExceptionGroup` |
| `await serial(name, *args, scope=None)` | `Any` | 按注册顺序等待；第一个既不是 `None`、也不是 `False` 的结果使派发停止；异常直接传播 |
| `bail(name, *args, scope=None)` | `Any` | 同步版短路；监听器返回 awaitable 时抛出 `TypeError` |
| `waterfall(name, *args, final=..., scope=None)` | `Any` | 按中间件调用 next 链，返回链的结果；可能是 awaitable，需要调用方等待 |

`serial()` / `bail()` 没有命中时返回 `None`。短路判断按对象身份判断 `None` / `False`，所以 `0`、`""`、空容器也会被当作有效结果返回。

`parallel()`、`serial()`、`bail()`、`waterfall()` 的同步回调异常直接通过各自调用路径处理，不保证额外写入 `ctx.errors`；已经创建的异步受管任务失败会记录错误。

### scope

| 值 | 监听器范围 |
| --- | --- |
| `None` | 同一运行时内所有仍有效的监听器；不会自动局限到发出事件的 Context |
| 一个 `Context` | 在该 Context 或其派生子树注册的监听器 |
| `Callable[[Context], bool]` | 以注册 Context 为参数筛选 |

`global_=True` 的监听器绕过以上范围过滤。Realm 服务隔离与事件 scope 是两套机制。

### waterfall

```python
def waterfall(
    name: str,
    *args: object,
    final: Callable[..., Any],
    scope: EventScope = None,
) -> Any: ...
```

每个监听器收到 `(*args, next)`。`next()` 沿用当前参数，`next(*new_args)` 改变传给后续监听器的参数；不调用 next 就终止链。最后调用 `final(*args)`，final 不接收 next。

下面的片段假设 `ctx` 是活跃 Context：

```python
from collections.abc import Callable


def double(value: int, next_: Callable[[int], int]) -> int:
    return next_(value * 2)


ctx.on("calculate", double)
result = ctx.waterfall("calculate", 3, final=lambda value: value + 1)
assert result == 7
```

异步中间件需要等待它调用的异步下游；如果结果是 awaitable，最外层调用方也需要等待。异步 final 在没有监听器包装时不会自动成为受管任务，需要调用方管理等待。

内部事件 `internal/status` / `internal/error` 的参数与异常说明见 [API 首页](../api.md#观测与异常)。
