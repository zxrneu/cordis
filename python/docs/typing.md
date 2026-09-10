# 类型化插件开发

入门请先看[使用教程](tutorial.md)，运行时关系见[核心概念](core-concepts.md)，完整公开接口见 [API 参考](api.md)。

公开 API 的类型会沿着 `@plugin → Component[Config, Context] → ctx.use() → Fiber[Config, Context]` 保留。配置更新、服务注册／访问和 `ctx.spawn()` 的返回值都可以接受静态检查。推荐把服务接口和键放在共享的 contracts 模块中。

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

`ctx.service("name")`、动态 `ctx.name`、字符串事件名、反射式 import 和配置文件保留动态能力；字符串服务读取仍返回 `Any`。需要类型保证的业务代码应使用服务键或 Context 描述符。`Entry` 是异构配置数据，`config` 为 `object`；代码内装配时用 `ctx.use(component, config)` 检查配置匹配，文件配置则通过 `validate` 校验。

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
