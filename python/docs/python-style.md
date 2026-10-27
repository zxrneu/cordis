# Python 接口与迁移约定

Python 版保留插件依赖、动态启停和资源清理的行为目标，接口采用 Python 的对象、上下文管理器和异步任务习惯。业务代码可以用普通函数和类实现，不必继承框架服务基类或学习内部 Effect 模型。

## 在装配处获取依赖

插件声明启动所需的服务，用 `ctx.service(KEY)` 获取实际对象，然后通过普通参数传入业务逻辑。共享服务键保留静态类型信息，基础 `Context` 即可使用。

```python
@plugin(inject=[VALIDATOR, STORE], provide=[IMPORTER])
def importer(ctx: Context, _config: None) -> None:
    validator = ctx.service(VALIDATOR)
    store = ctx.service(STORE)
    ctx.provide(IMPORTER, CsvImporter(ctx, validator, store))
```

这里的业务类型和服务键来自订单导入示例。`CsvImporter` 显式接收校验与存储对象；Context 只承担它需要的任务管理。普通对象经 `provide()` 发布后保持身份，不会自动改写方法调用时的上下文。

需要为调用者启动工作或登记资源时，把所有者作为明确参数传给服务方法。提供方自己的后台工作使用提供方的 Context，调用方的工作使用调用方的 Context。依赖关系仍由插件声明维护，资源释放前会先等待相关调用方退出。

## 取得资源后立即交给作用域管理

`enter_context()` 和 `enter_async_context()` 接受标准上下文管理器，返回进入操作的实际结果，并登记退出操作：

```python
from pathlib import Path

from cordis import Context, plugin


@plugin
def audit(ctx: Context, path: Path) -> None:
    stream = ctx.enter_context(path.open("a", encoding="utf-8"))
    ctx.on("audit", lambda message: stream.write(message + "\n"))
```

文件保持打开直到插件退出。卸载时先让在途任务结束，再按登记顺序逆序释放资源。如果后续初始化失败，已登记的资源同样会被释放。只有清理方法的对象可以用标准库 `closing()` / `aclosing()` 包装，已有清理函数可用 `ctx.defer()` 登记。

每次取得资源或完成注册后立即登记清理。先批量完成多个注册、最后返回一组清理函数，会在中途失败时遗漏前面成功的注册。

这两个方法负责延长资源生命周期，退出时向管理器传入的异常信息为空。需要让管理器观察或处理代码块异常时，使用普通 `with` / `async with`；不要把延迟清理当作跨任务的异常处理机制。异步进入过程自身的部分获取和取消处理仍由管理器负责；进入成功后如果所属作用域已经关闭，运行时会立即释放资源并拒绝登记。

## 用代码块表达局部生命周期

```python
import asyncio

from cordis import Context


async def main() -> None:
    async with Context() as root:
        received: list[int] = []
        async with root.scope() as scope:
            scope.on("item", received.append)
            root.emit("item", 1)
        root.emit("item", 2)
        assert received == [1]


asyncio.run(main())
```

通过 `scope` 注册的监听器、服务、任务和子插件归该作用域管理；退出时等待任务结束并清理资源。通过 `root` 注册的内容仍归 `root` 管理，不会仅因执行位置在代码块内而改变归属。作用域共享父 Context 的服务依赖，退出后不能再用它创建工作或资源。

局部作用域不会创建新的插件或隔离域；它用于缩短资源生命周期。父插件退出或整个运行时关闭时，也会清理尚未退出的局部作用域。取消作用域的宿主任务会等待清理完成，再传播取消。

## 兼容接口的边界

已有接口继续支持，迁移可以逐步完成：

| 原有写法 | 推荐写法 |
| --- | --- |
| 动态 `ctx.database` 或服务描述符 | 装配处调用 `ctx.service(DATABASE)`，业务对象显式接收依赖 |
| 服务自动绑定调用者、动态改变 `self.ctx` | 普通对象；需要调用者归属时显式传入所有者 |
| `yield cleanup` 或返回一组清理函数 | 获取资源后立即 `enter_context()` / `enter_async_context()` / `defer()` |
| `await ctx.effect(callback)` 管理局部生命周期 | `async with ctx.scope() as scope` |
| 在业务对象中到处查找服务 | 在插件装配边界解析依赖，通过构造参数传入 |

标准 `@contextmanager` / `@asynccontextmanager` 插件也继续支持，其 `yield` 保持 Python 上下文管理器语义：卸载时才继续执行退出逻辑。原始生成器插件的 `yield cleanup` 属于旧的初始化协议，会在启动阶段继续迭代，两者不要混用。

`Service`、`ContextAware`、`BoundService`、动态属性及 `accessor/mixin` 保留既有行为，用于需要这些语义的集成。新业务代码优先采用上面的显式方式。内部的 Effect、Sink 和 Fiber 仍用于调度、所有权记录和诊断，无需为了语言风格重命名这些内部结构。
