# 类型化插件开发

入门请先看[使用教程](tutorial.md)，运行时关系见[核心概念](core-concepts.md)，完整公开接口见 [API 参考](api.md)。

已有项目从 Fiber 状态查询、字符串事件或动态 Loader 迁移时，先看[类型化接口迁移指南](migration-typed-interfaces.md)，其中列出了需要改动的调用点和兼容边界。

公开 API 的类型会沿着 `@plugin → Component[Config, Context] → ctx.use() → Fiber[Config, Context]` 保留，也可以通过 `configure() → BoundComponent → Loader.mount()` 保留。配置更新、服务注册／访问、类型化事件和 `ctx.spawn()` 的返回值都可以接受静态检查。推荐把服务接口和键放在共享的 contracts 模块中。

## 服务接口、配置与插件

```python
from dataclasses import dataclass
from typing import Protocol

from cordis import Context, ServiceKey, plugin


class Greeter(Protocol):
    def greet(self, name: str) -> str: ...


GREETER = ServiceKey[Greeter]("greeter")


class AppContext(Context):
    greeter = GREETER


@dataclass(frozen=True)
class Config:
    prefix: str


@dataclass
class Greeting:
    prefix: str

    def greet(self, name: str) -> str:
        return f"{self.prefix} {name}"


@plugin(provide=[GREETER])
def provider(ctx: AppContext, config: Config) -> None:
    ctx.provide(GREETER, Greeting(config.prefix))


@plugin(inject=[GREETER])
def consumer(ctx: AppContext, _config: None) -> None:
    print(ctx.greeter.greet("Cordis"))


async def main() -> None:
    async with AppContext() as root:
        root.use(consumer)
        fiber = root.use(provider, Config(prefix="你好"))
        await root.flush()
        await fiber.update(Config(prefix="Hello"))
```

`provider` 的推断类型是 `Component[Config, AppContext]`，`fiber` 是 `Fiber[Config, AppContext]`，`fiber.config` 是 `Config`，`fiber.ctx` 是 `AppContext`。传入错误配置、遗漏必需配置、传错 Context 类型，都会产生类型错误。没有配置的插件用 `_config: None`；同步函数、异步函数、生成器和异步生成器都保留这条类型链。

装饰器必须能看见回调的参数注解。显式写成 `Any` 或完全没有注解的旧插件仍可运行，但不能提供相同的静态保证。

`validate` 的类型是 `Config → Config | Awaitable[Config]`，更新 hook 是 `(Context, Config, Config) → object`，因此校验器和插件的配置类型也需要一致。加载配置文件时的 JSON/TOML 解码仍是动态边界，需要运行时校验。

## 服务键与属性提示

`ServiceKey[T]` 的 `T` 不变（invariant），因此检查器不能把 `ServiceKey[int]` 和一个字符串值一起拓宽成 `object` 来接受错误注册。

| 操作 | 静态结果 |
| --- | --- |
| `ctx.provide(GREETER, value)` / `ctx.set(GREETER, value)` | `value` 必须符合 `Greeter` |
| `ctx.service(GREETER)` / `ctx[GREETER]` | `Greeter` |
| `ctx.get(GREETER)` | `Greeter \| None` |
| `ctx.get(GREETER, default)` | `Greeter \| 默认值类型` |
| `ctx.greeter`（上面的 AppContext） | `Greeter`，只读描述符 |
| `ctx.isolate(GREETER)` / `ctx.intercept(GREETER, metadata)` | 保留 `AppContext` 类型 |
| `ctx.spawn(coroutine)` | 保留协程结果类型的 `asyncio.Task[T]` |

服务键同样用于 `inject`、`provide`、`isolate`、`intercept`、`metadata` 和 `notify`。运行时仍通过原有的声明检查、Realm 和 committed view 解析服务，不会因为用了属性描述符就绕过依赖检查。

键使用现有的字符串命名空间：`ServiceKey[int]("count")` 与字符串 `"count"` 指向同名服务。请从同一个 contracts 模块导入键，避免为同一名字定义不兼容接口。泛型参数不会自动验证 Python 对象，类型检查也不是运行时沙箱。

`ctx.service("name")`、动态 `ctx.name`、字符串事件名、反射式 import 和配置文件保留动态能力；字符串服务读取仍返回 `Any`。需要类型保证的业务代码应使用服务键或 Context 描述符。`Entry` 是动态配置数据，`config` 为 `object`；它不能在静态检查中证明插件与配置匹配，类型化装配使用下面的绑定入口。

## 配置管理与运行视图

`ctx.fiber` 是 `FiberView[Self]`，`Loader[AppContext].get()` / `reload()` 返回 `FiberView[AppContext]`（get 还可能返回 None）。运行时列表和错误归属中的视图保留基础 Context 类型。视图的属性只读，配置未知时读取为 object，不暴露 update；直接等待或调用 wait 后仍是视图。

```python
fiber = root.use(provider, Config("你好"))
await fiber.update(Config("Hello"))  # 保留 Config
view = fiber.ctx.fiber
await view.restart()                # 不需要知道配置类型
# await view.update(123)             # 类型错误：视图没有 update
```

这解决了 `Fiber[object, Context]` 会虚假允许任意配置写入的问题。现有管理代码可继续读取状态、等待、重启和卸载；更新配置的代码需保存初次挂载返回的类型化 Fiber。视图使用同一个运行时对象，不深度冻结配置，不自动阻止未经类型检查的 Python 调用。

`fiber.tasks` 是不同结果类型的任务快照，结果为 object，而单次 `ctx.spawn()` 返回的 Task[T] 仍保留 T。

## 先绑定配置，再交给 Loader

```python
from cordis import Loader

loader = Loader(root)
binding = provider.configure(Config("你好"))
fiber = await loader.mount("provider", binding)  # Fiber[Config, AppContext]
await fiber.update(Config("Hello"))
await loader.unmount("provider")
```

`configure()` 检查配置与插件匹配，返回不可变的 `BoundComponent[Config, AppContext]`，尚不创建 Fiber。Loader 执行完整绑定后只保存生命周期视图，不再把插件与配置拆成可以任意组合的公开字段。接收基础 Context 的插件也可挂载到 AppContext，返回句柄仍保留 AppContext；反向组合会被类型检查拒绝。

外部配置先解码为业务类型：

```python
from collections.abc import Mapping


def decode_config(raw: object) -> Config:
    if not isinstance(raw, Mapping):
        raise ValueError("配置必须是映射")
    prefix = raw.get("prefix")
    if not isinstance(prefix, str):
        raise ValueError("prefix 必须是字符串")
    return Config(prefix)


binding = await provider.decode({"prefix": "你好"}, decode_config)
fiber = await loader.mount("provider", binding)
```

decoder 可同步或异步，输入 object、输出 Config。decode 不创建挂载；转换失败时不会影响运行中的插件。插件的 validate 继续接收 Config，在激活和更新时检查业务约束。

原有 `Entry` / `reconcile()` 继续用于动态清单与字符串模块定位符；mount 条目由 `unmount()` 或 `close()` 管理，不进入 entries，也不会被 reconcile 的完整清单移除。两种入口共享 id 范围；重复 id 会被拒绝。类型化 mount 当前重启已有组件定义，源码 HMR 仍走动态 Entry 路径。

## 类型化通知与查询

```python
from cordis import EventKey, QueryKey

MESSAGE = EventKey[str]("message")
FIND_USER = QueryKey[int, str]("find_user")


def receive(message: str) -> None:
    print(message)


async def find_user(user_id: int) -> str | None:
    return "Cordis" if user_id == 1 else None


root.on(MESSAGE, receive)
root.emit(MESSAGE, "hello")
root.handle(FIND_USER, find_user)
name = await root.query(FIND_USER, 1)  # str | None
```

`EventKey[T]` 将一个通知参数与回调参数关联，支持 on、once、emit、parallel。回调结果为 None 或 Awaitable[None]；多个参数可打包为 dataclass。`QueryKey[Request, Result]` 将请求和结果关联，支持同步或异步处理器，调用返回 Result | None。同一运行时每个查询键只允许一个处理器；无有效处理器或未找到结果时返回 None，False 是正常结果。

这些键的类型参数不变，检查器不能拓宽为 object 来放行错误的回调、参数或结果。键按对象身份匹配，名称仅用于诊断：请共享同一个键对象，不要在两边各自构造同名键。它们不与字符串事件互通，也不采用 ServiceKey 的字符串别名规则。异步通知与查询任务均归属注册方的 Fiber / Effect，遵守其卸载策略。

迁移时把业务通知名替换为共享 EventKey；有结果的单处理器请求使用 QueryKey 与 handle/query。现有字符串 serial、bail、waterfall 的动态策略保留，不能直接传入上述键。完整可运行示例：[`typed_interfaces.py`](../examples/typed_interfaces.py)，运行 `uv run python -m examples.typed_interfaces`。

## 保留的动态边界

字符串事件的 `bail()`、`serial()`、`waterfall()` 结果，以及 `Entry.intercept` 的值也为 object。object 要求使用前确认类型，而 Any 会放行未知成员访问。例如：

```python
answer = ctx.bail("answer")
if isinstance(answer, int):
    print(answer.bit_length())
```

这改变的是静态检查，运行时派发与元数据内容保持原样。字符串服务读取仍是兼容旧插件的动态边界；这些收紧不会使任意动态插件自动获得类型安全。

## TypedDict 与类插件

泛型函数中的字典字面量可能先被推断成 `dict`。使用 `TypedDict` 配置时，先给变量显式注解，再传给 `ctx.use()`：

```python
from typing import TypedDict


class Options(TypedDict):
    prefix: str


@plugin
def configured(ctx: Context, config: Options) -> None:
    pass


options: Options = {"prefix": "hello"}
fiber = Context().use(configured, options)
```

类插件要获得可靠的 mypy 推断，使用显式工厂调用：

```python
class Server:
    def __init__(self, ctx: Context, config: Options) -> None:
        self.config = config

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass


server = plugin(Server)
```

`server` 被推断为 `Component[Options, Context]`。`@plugin` 直接装饰类仍兼容运行时，但 mypy 不会把类装饰器的返回类型作为被装饰名字的类型；类型化代码使用上面的显式调用。

## 验证类型契约

```bash
cd python
uv sync
uv run mypy
uv run python -m unittest discover -v
uv run ruff check src tests examples typing_tests
```

项目启用 mypy `strict`，检查整个 `src`、可运行示例和 `typing_tests/contracts.py`。后者通过 `assert_type` 验证推断结果。`typing_tests/invalid.py` 有明确标记的错误用法，`tests/test_typing.py` 要求每个标记位置都出现诊断；如果以后某个兜底 `Any` 让错误静默通过，测试会失败。核心运行时仍不依赖第三方库，mypy 只属于开发依赖。
