# Cordis Python 中文指南

本文从 Cordis 的设计动机讲到 Python 实现、插件开发、配置加载、热重载、生产使用边界和后续路线。对应实现基于论文 [A Programming Paradigm for Spatiotemporal Composability](https://arxiv.org/abs/2608.25512)，但不是论文中 TypeScript Cordis 的逐行翻译。

## 1. Cordis 解决什么问题

普通插件系统通常只解决“怎么加载”，没有完整解决下面两个问题：

1. 插件卸载后，注册的事件、服务、子插件和资源能否全部撤销？
2. 插件依赖的服务出现、消失或换了提供者时，插件能否自动调整生命周期？

Cordis 把它们分成两个相互正交的维度：

- **时间可组合性**：插件产生的每个 Effect 都携带逆操作，卸载时按相反顺序恢复。
- **空间可组合性**：插件声明需要的 Coeffect；运行时根据依赖是否满足，自动激活或停用插件。

两者通过同一个 `Context` 统一。插件不直接修改全局注册表，而是通过 `Context` 注册服务、事件和子插件；这样运行时才能知道“谁创建了什么、谁依赖谁、应该以什么顺序撤销”。

## 2. 核心模型

### 2.1 Component 与 Fiber

`Component` 是插件定义，描述一类可加载能力；`Fiber` 是它的一次运行实例。

```python
from cordis import plugin


@plugin(inject=["database"], provide=["repository"])
def repository(ctx, config):
    ...
```

这段声明表示：

- `repository` 运行前必须能解析到 `database`；
- 它承诺在加载期间提供 `repository` 服务；
- 如果承诺没有兑现，Fiber 会进入 `FAILED`，已经产生的 Effect 会被回滚。

Fiber 状态机如下：

```text
             依赖满足                    加载成功
PENDING --------------------> LOADING --------------------> ACTIVE
   ^                              |                           |
   |                              | 加载失败                  | 依赖变化/重启/卸载
   |                              v                           v
   +--------------------------- FAILED                  UNLOADING
                                                              |
                                仍需要且依赖满足  -> LOADING   |
                                仍需要但依赖缺失  -> PENDING   |
                                不再需要          -> DISPOSED  |
```

每个 Fiber 有自己的异步转换任务 `inertia`。某个插件加载很慢时，不会阻塞无关插件；同一个 Fiber 的加载、卸载和重载则被串行化，避免同一实例同时处于两次转换中。

### 2.2 可逆 Effect

Effect 是“动作 + 逆操作”。最简单的插件返回一个 cleanup：

```python
@plugin
def listener(ctx, _config):
    handle = external_bus.subscribe("message", print)
    return handle.unsubscribe
```

多个操作按创建顺序记录，卸载时按 LIFO 顺序撤销：

```python
@plugin
async def app(ctx, _config):
    await ctx.effect(lambda: acquire_a())  # 返回 release_a
    await ctx.effect(lambda: acquire_b())  # 返回 release_b

# 卸载顺序：release_b -> release_a
```

`ctx.effect()` 接受同步或异步函数、同步或异步迭代器。迭代器每次产出一个 cleanup；运行时在进入下一步之前就保存它。因此初始化到一半失败，前面已经完成的步骤仍能正确回滚。

```python
def initialize():
    first = acquire_first()
    yield first.close

    second = acquire_second()  # 即使这里抛错，first.close 仍会运行
    yield second.close
```

cleanup 是插件作者和运行时之间的契约：运行时保证保存、排序和至多执行一次，但无法证明 cleanup 真的恢复了原动作。

### 2.3 响应式 Coeffect

服务提供者把值放进 Context：

```python
@plugin(provide=["clock"])
def clock(ctx, _config):
    ctx.provide("clock", time.monotonic)
```

消费者显式声明并读取：

```python
@plugin(inject=["clock"])
def metrics(ctx, _config):
    started_at = ctx.clock()
```

运行时提交的不是服务值快照，而是“服务由哪个 Fiber 提供”的视图。提供者 UID 不复用，所以即使新旧服务值相等，更换提供者也会触发消费者卸载和重新加载。

访问规则：

- 插件应先在 `inject` 中声明，再使用 `ctx.name` 或 `ctx["name"]`；
- 未声明访问会抛出 `UndeclaredServiceError`；
- 已声明但当前没有可用依赖会抛出 `InactiveServiceError`；
- `ctx.get()` 是反射式查询，适合框架代码和可选探测，不提供声明检查。

### 2.4 为什么卸载顺序安全

提供服务的 Fiber 离开 `ACTIVE` 时，运行时先把它标记为 `UNLOADING`。此后新消费者不能再解析到它，但已有消费者保留自己的 committed view：

```text
提供者标记 UNLOADING
        |
        +-- 新消费者不再获得该服务
        |
        +-- 通知已有消费者卸载
                  |
                  +-- 消费者运行 cleanup，期间仍能读取原服务
                  |
                  +-- 消费者进入 PENDING / DISPOSED
        |
提供者确认依赖者均已退出
        |
提供者运行 cleanup 并撤销服务
```

因此，消费者 teardown 可以安全调用自己已经提交的依赖。提供者不会在消费者之前关闭连接。

这个保证的粒度是 **Fiber 生命周期**，不是任意 Python 对象引用。裸 `asyncio.create_task()` 不会自动登记到 Fiber；详见“在途任务”。

### 2.5 Context、隔离域和拦截

Context 同时承担三种职责：

- Effect 所有权：注册内容自动跟随创建它的 Fiber；
- Coeffect 解析：按服务名和 Realm 查找提供者；
- 层级关系：父插件卸载时，子插件随之卸载。

默认情况下，同名服务共享一个 Realm。同一 Realm 只能有一个提供者。`isolate()` 或 Loader 的 `isolate` 可以让同名服务拥有多份实例：

```python
left = root.isolate("database")
right = root.isolate("database")

left.use(database, {"dsn": "left.db"})
right.use(database, {"dsn": "right.db"})
```

拦截 metadata 用于改变“同一个服务在特定消费者眼中如何使用”，而不复制服务：

```python
@plugin(provide=["storage"])
def storage(ctx, _config):
    ctx.provide(
        "storage",
        backend,
        adapt=lambda value, metadata, _ctx: value.view(**(metadata or {})),
    )


@plugin(inject={"storage": {"readonly": True}})
def report(ctx, _config):
    readonly_storage = ctx.storage
```

## 3. Python 实现

### 3.1 代码结构

| 文件 | 职责 |
| --- | --- |
| `src/cordis/core.py` | Component、Effect、Fiber、Context、服务、Realm、事件 |
| `src/cordis/loader.py` | Entry、配置树增量协调、嵌套组、配置文件监听 |
| `src/cordis/hmr.py` | Python 模块依赖图、过期 Entry 检测、事务式 HMR |
| `src/cordis/__init__.py` | 公共 API |
| `tests/` | 生命周期、失败回滚、隔离、Loader 与 HMR 测试 |

实现只要求 Python 3.11+，核心没有第三方依赖。YAML 配置需要可选的 PyYAML。

### 3.2 Effect 执行器

内部 `_execute()` 把不同 Python 形态归一化：

- 普通返回值：`None` 或 cleanup callable；
- coroutine：等待后处理其返回值；
- generator / async generator：逐项登记 cleanup；
- context manager / async context manager：进入后登记退出操作；
- 嵌套 `ctx.effect()`：作为父 Effect 的一个逆操作。

每次迭代前会检查 guard。组件目标发生变化时，不再启动下一步；当前已经开始的异步步骤遵循惯性语义，运行完成后立刻回滚。

### 3.3 Fiber 目标与提交视图

Fiber 的 target 由两部分组成：

```text
(配置 revision, 每个声明依赖的 provider UID)
```

target 变化意味着配置或依赖身份变化。Fiber 会经过 `UNLOADING`，撤销旧 Effects，再根据最新 target 决定重新加载还是等待。

加载开始时，Fiber 将依赖解析结果复制进 `committed`。直到它的 cleanup 全部完成才清空，因此 teardown 看到的依赖不会在中途漂移。

### 3.4 失败模型

- 初始化失败：撤销已经登记的 Effect，Fiber 进入 `FAILED`；
- cleanup 失败：其余 cleanup 继续执行，最终以 `ExceptionGroup` 汇总；
- `restart()`：清除失败结果并增加 revision；
- `update()`：新配置加载失败时，默认重新加载上一个有效配置；
- 一个 Fiber 失败不会自动终止兄弟 Fiber。

可以监听内部事件或读取快照：

```python
root.on("internal/status", lambda fiber, old: print(old, "->", fiber.state))
root.on("internal/error", lambda error, fiber: print(fiber, error))

print(root.fibers)
print(root.errors)
```

## 4. 编写插件

### 4.1 安装和最小示例

在本仓库中开发：

```bash
cd python
python -m pip install -e .
```

```python
import asyncio
from cordis import Context, plugin


@plugin(provide=["greeter"])
def greeter(ctx, config):
    ctx.provide("greeter", lambda name: f"{config['prefix']} {name}")


@plugin(inject=["greeter"])
def app(ctx, _config):
    print(ctx.greeter("Cordis"))


async def main():
    async with Context() as root:
        root.use(app)  # 依赖尚未满足，暂时 PENDING
        root.use(greeter, {"prefix": "你好"})
        await root.flush()


asyncio.run(main())
```

### 4.2 支持的插件形态

同步函数：

```python
@plugin
def sync_plugin(ctx, config):
    resource = open_resource(config)
    return resource.close
```

异步函数：

```python
@plugin
async def async_plugin(ctx, config):
    resource = await open_resource(config)

    async def close():
        await resource.close()

    return close
```

Effect 迭代器：

```python
@plugin
def staged_plugin(ctx, _config):
    first = acquire_first()
    yield first.close
    second = acquire_second()
    yield second.close
```

类插件可以实现 `start()`，并实现 `stop()` 或 `close()`：

```python
@plugin
class Server:
    def __init__(self, ctx, config):
        self.port = config["port"]

    async def start(self):
        ...

    async def stop(self):
        ...
```

### 4.3 配置校验和原地更新

`validate` 在加载和更新前运行，可以返回规范化后的配置：

```python
def validate(config):
    port = int(config["port"])
    if not 1 <= port <= 65535:
        raise ValueError("port 超出范围")
    return {**config, "port": port}


@plugin(validate=validate)
def server(ctx, config):
    ...
```

默认更新会完整卸载并重载。只有确实能安全原地修改时才提供 `update`：

```python
async def update_server(ctx, previous, current):
    await ctx.server.change_limit(current["limit"])


@plugin(inject=["server"], update=update_server)
def limiter(ctx, config):
    ...
```

原地 update 的副作用由 update 自己负责；当前实现不会为它自动生成回滚操作。

### 4.4 子插件

在插件内部调用 `ctx.use()`，子 Fiber 会归属于父 Fiber：

```python
@plugin
def parent(ctx, _config):
    ctx.use(child_a)
    ctx.use(child_b)
```

父插件卸载时，两个子插件先卸载。不要把父插件的 Context 保存给父生命周期以外的任务使用；运行时会拒绝失效 Context 创建新的 Effect。

### 4.5 事件

```python
dispose = ctx.on("message", handler)
ctx.once("ready", initialize_once)

ctx.emit("message", payload)          # 同步触发；异步结果转为后台 Task
await ctx.parallel("message", payload) # 并行等待并聚合异常
result = await ctx.serial("query", payload)
result = ctx.bail("sync-query", payload)
```

事件监听器本身是可逆 Effect，插件卸载会移除监听器。已经开始执行的异步监听器目前不会自动归属于监听器 Fiber，这是一个已知边界。

## 5. 在途任务与安全卸载

### 5.1 当前保证

Cordis 能自动排空依赖 Fiber，但不能从 Python 运行时反向发现某个 `asyncio.Task` 正在持有哪个服务。

如果插件创建后台任务，必须返回一个会停止接收新任务并等待或取消存量任务的 cleanup：

```python
import asyncio
import contextlib


@plugin(inject=["database"])
def worker(ctx, _config):
    tasks: set[asyncio.Task] = set()
    accepting = True

    async def execute(job):
        await ctx.database.execute(job)

    def submit(job):
        if not accepting:
            return
        task = asyncio.create_task(execute(job))
        tasks.add(task)
        task.add_done_callback(tasks.discard)

    ctx.on("job", submit)

    async def cleanup():
        nonlocal accepting
        accepting = False
        await asyncio.gather(*tuple(tasks))  # drain 策略

    return cleanup
```

如果任务不应继续运行，cleanup 可以改为 cancel 策略：

```python
async def cleanup():
    for task in tasks:
        task.cancel()
    for task in tuple(tasks):
        with contextlib.suppress(asyncio.CancelledError):
            await task
```

### 5.2 生产环境建议

- 在 cleanup 的第一步关闭入口，例如把 `accepting` 设为 `False`；
- drain 必须设置业务级超时，避免一个永不返回的请求永久阻塞卸载；
- 数据库事务、文件写入和消息确认应由业务服务提供明确的取消或提交语义；
- 不要 fire-and-forget；至少保存 Task 引用并消费异常；
- 滚动升级时先启动新提供者，把新流量切过去，再排空旧提供者。

仓库中的 [`examples/graceful_unload.py`](../examples/graceful_unload.py) 演示了服务重载时排空在途请求。

### 5.3 DSH-like Agent Harness Demo

[`examples/dsh_like.py`](../examples/dsh_like.py) 用同一套 Cordis 原语组装了一个离线 Agent Harness：

```text
headless app -> agent loop -> llm
                         |-> tool registry -> policy
                         |-> append-only session log
                         |-> system prompt

tool plugins -> calculator / memory / uppercase
```

模型适配器、工具注册表、策略、会话日志、记忆、System Prompt、Agent Loop、具体工具和 headless app 全部是插件。Demo 还会更新 policy Entry：第一次 `uppercase` 调用被拒绝；配置 patch 后依赖链自动重载，第二次调用成功。Session 插件没有重载，因此更新前后的事件保留在同一份 append-only log 中。

它刻意使用确定性的 `FakeDeepSeek`，用于验证 Harness 组装和生命周期，不需要网络或 API Key。真实模型只需替换 `llm` 服务提供者，不需要修改 Agent Loop。

### 5.4 Cordis Agent：连接真实 DeepSeek API

[`examples/cordis_agent/`](../examples/cordis_agent/) 是可以实际对话和调用工具的模块化版本。它借鉴 DSH 的 Everything-is-a-Plugin 架构，但使用自己的项目名。`llm.py`、`session.py`、`tools.py`、`agent.py`、`plugins.py` 和 `__main__.py` 分别承载模型适配、会话、工具运行时、Agent Loop、Cordis 组装与 CLI。它使用 DeepSeek 的 OpenAI-compatible Chat Completions API，保存原生 assistant/tool message 协议，并提供：

- `calculator`：安全四则运算；
- `list_files` / `read_file`：访问指定 workspace；
- `write_file`：限制在 workspace 内，交互模式下每次请求人工批准；
- `remember` / `recall`：进程内记忆；
- append-only JSONL Session，可通过 `--resume` 恢复。

所有公开 I/O 路径都是异步接口：DeepSeek HTTP 请求直接使用 asyncio TCP/TLS stream，超时或取消会关闭连接；Session、workspace、配置读取、HMR 扫描和终端输入不会在事件循环中执行阻塞 I/O。普通磁盘文件和跨平台终端没有可移植的原生 asyncio API，因此这两类操作通过 `asyncio.to_thread()` 委托；网络 I/O 不使用线程。

```bash
cd python
export DEEPSEEK_API_KEY="你的 API Key"

# 交互运行
PYTHONPATH=src python -m examples.cordis_agent --workspace .

# 单次/脚本运行；修改型工具会被拒绝
PYTHONPATH=src python -m examples.cordis_agent \
  --workspace . \
  --once "阅读 README.md 并概括这个项目"
```

可用环境变量和参数：

| 配置 | 默认值 | 说明 |
| --- | --- | --- |
| `DEEPSEEK_API_KEY` / `--api-key` | 无 | 必填，不写入 Session |
| `DEEPSEEK_BASE_URL` / `--base-url` | `https://api.deepseek.com` | 可替换为兼容端点 |
| `DEEPSEEK_MODEL` / `--model` | `deepseek-v4-flash` | 模型名称 |
| `--thinking` | 关闭 | 启用 thinking mode；reasoning content 会原样保留在会话中 |
| `--session` | `.cordis-agent/session.jsonl` | Session 文件 |
| `--resume` | 关闭 | 读取并延续原 Session |
| `--once TEXT` | 无 | 非交互运行，可重复 |

这是能真实执行的最小 Harness，不是安全沙箱。路径检查只能限制文件工具；不可信插件仍应放进独立进程或容器。

## 6. 声明式 Loader

### 6.1 Entry

```python
from cordis import Entry, Loader

loader = Loader(root)
await loader.reconcile([
    Entry(
        id="database",
        plugin="myapp.plugins:database",
        config={"dsn": "app.db"},
        isolate={"database": "tenant-a"},
    ),
    Entry(
        id="api",
        plugin="myapp.plugins:api",
        intercept={"database": {"readonly": True}},
        isolate={"database": "tenant-a"},
    ),
])
```

字段说明：

| 字段 | 含义 |
| --- | --- |
| `id` | 稳定身份；同一层必须唯一 |
| `plugin` | Component、callable，或 `package.module:attribute` |
| `config` | 传给插件的配置 |
| `enabled` | 保留 Entry 但停用 Fiber |
| `isolate` | `True` 创建 Entry 私有 Realm；字符串复用命名 Realm |
| `intercept` | 当前 Entry Context 的服务 metadata |
| `children` | 创建嵌套配置组；不能和 `plugin` 同时使用 |

`reconcile()` 按 id 增量处理：新增、删除、启停、配置更新、插件替换和 Realm 迁移。没有变化的 Entry 保留原 Fiber。

### 6.2 配置文件

JSON/YAML 根节点是 Entry 列表：

```json
[
  {
    "id": "database",
    "plugin": "myapp.plugins:database",
    "config": {"dsn": "app.db"}
  },
  {
    "id": "features",
    "children": [
      {"id": "api", "plugin": "myapp.plugins:api"}
    ]
  }
]
```

TOML 使用数组表：

```toml
[[plugins]]
id = "database"
plugin = "myapp.plugins:database"

[plugins.config]
dsn = "app.db"
```

加载和监听：

```python
await loader.load_file("cordis.toml")
await loader.watch_file("cordis.toml", interval=0.5, stop=stop_event)
```

`watch_file()` 是简单轮询器。生产环境应在外围负责日志、退避、配置发布原子性和进程退出信号。

## 7. 热模块替换

HMR 的流程是：

1. 从已加载 Python 源模块构建静态 import 图；
2. 从变更文件沿反向依赖找到受影响模块；
3. 找出插件模块受影响的 Loader Entry；
4. 备份并清除相应的 `sys.modules` 和字节码缓存；
5. 导入新模块，卸载旧 Fiber，启动新 Fiber；
6. 导入或启动失败时恢复模块缓存，并用旧 Component 重建 Fiber。

```python
reloaded = await loader.reload_changed(
    ["myapp/shared.py"],
    externals={"native_extension", "process_singleton"},
)
```

变更跨越 `externals` 边界时抛出 `FullRestartRequired`，调用者应执行进程级重启。

HMR 是运行时事务，不是数据库事务。模块 import 阶段写文件、发网络请求或修改第三方全局状态时，恢复 `sys.modules` 无法撤销这些外部变化。插件模块应尽量保持 import-time 纯净，把副作用放进 Component 并返回 cleanup。

## 8. 应用架构

### 8.1 推荐分层

一个实际应用可以分为：

```text
进程入口
  └─ Loader
      ├─ 基础设施提供者：database / cache / http_client
      ├─ 领域服务：user_repository / billing
      ├─ 接入层：http_server / bot_adapter / worker
      └─ 可选功能组：metrics / admin / experimental
```

设计原则：

- 服务接口表达能力，不暴露提供者 Fiber；
- 基础设施插件负责资源生命周期，消费者只声明依赖；
- 共享同一后端用 intercept 生成受限视图；需要独立实例才使用 isolate；
- 一个插件只管理能一起加载和卸载的资源；
- 循环依赖应通过提取更小的公共服务或引入 broker 打破。

### 8.2 适合的场景

- Bot、IDE、数据处理器等长运行插件宿主；
- Agent harness 中可动态替换的工具、记忆和权限组件；
- 测试期间按用例装卸基础设施；
- 单进程多租户环境中的隔离服务实例；
- 需要保留进程局部状态的开发期热重载。

### 8.3 不适合单独承担的场景

- 不可信第三方代码：需要独立进程、容器或解释器级沙箱；
- 必须撤回已发送邮件、网络响应等外部 emission：需要业务补偿或幂等协议；
- 原生扩展和全局单例无法可靠重载：应划为 HMR external；
- 强一致持久化回滚：应使用数据库事务或事件溯源，而不是依赖内存 cleanup。

## 9. 当前实现的瑕疵

下面列的是已知事实，不是论文模型本身的保证：

| 优先级 | 瑕疵 | 影响 / 临时做法 |
| --- | --- | --- |
| P0 | 没有 `ctx.spawn()`；裸 Task 和异步 `emit()` 回调不归属 Fiber | 插件手动保存 Task，在 cleanup 中 drain/cancel |
| P0 | cleanup 没有统一超时 | 业务 cleanup 自己使用 `asyncio.timeout()` |
| P1 | Loader reconcile 不是全局事务 | 一次更新中途失败可能留下已完成的增删；发布前校验配置，失败后重新 reconcile 已知良好配置 |
| P1 | HMR 依赖图只识别 AST 中的静态 import | `importlib` 动态导入、运行时路径修改、部分 namespace package 需要显式 reload 或完整重启 |
| P1 | HMR 只恢复模块缓存和 Component，不撤销 import-time 外部副作用 | 保持模块顶层纯净，把副作用放进插件生命周期 |
| P1 | 没有依赖环诊断 | 循环依赖会保持 `PENDING`；通过状态事件和 `fibers` 排查 |
| P1 | 服务类型主要是 `Any` | 使用 Protocol、类型检查器和项目内封装补强，运行时仍做声明检查 |
| P2 | 每个 Realm 只允许一个直接提供者 | 需要多实例时增加 broker/multiplexer 服务 |
| P2 | `watch_file()` / `HotReloader.watch()` 只有轮询，没有 debounce、退避和错误恢复策略 | 由进程入口监督 watcher |
| P2 | Realm 缓存和错误历史没有回收策略 | 长时间高频创建动态 Entry 时可能增长 |
| P2 | ModuleGraph 扫描已加载模块并读取源码 | 大型进程中 HMR 检测延迟会随模块数增长 |
| 边界 | 任意全局变量、泄漏线程、外部进程和不可逆 emission 不受 Context 控制 | 使用进程隔离、事务、幂等和补偿操作 |

## 10. TODO

### P0：任务所有权与可终止卸载

- [ ] 增加 `ctx.spawn(awaitable, *, policy="drain", timeout=None)`；
- [ ] Task 自动继承当前 Fiber、收集异常并在 Fiber unload 时停止接收；
- [ ] 支持 `drain`、`cancel` 和超时后 cancel；
- [ ] 让异步 `emit()` 自动使用监听器所属 Fiber 的任务域；
- [ ] 添加“提供者卸载时，在途消费者完成后才关闭服务”的竞态测试。

验收标准：插件不需要维护 Task set；卸载不会遗留 Task；超时不会永久卡住 Fiber；任务异常进入 `internal/error`。

### P1：配置事务和诊断

- [ ] Loader 在 mutation 前完成导入、Entry 校验和 Realm 冲突预检；
- [ ] reconcile 失败时恢复旧 Entry/Fiber 集合；
- [ ] 检测依赖环，并输出涉及 Fiber、服务名和 Realm 的诊断；
- [ ] 给失败状态增加结构化原因和最近一次转换记录；
- [ ] 为 update hook 定义显式 rollback 契约。

验收标准：任意 Entry 更新失败后，原配置仍全部可用；循环依赖不再表现为无解释的永久 PENDING。

### P1：HMR 完整性

- [ ] 缩小 ModuleGraph 扫描范围并缓存解析结果；
- [ ] 支持显式声明动态依赖和 decline/accept 边界；
- [ ] 对 namespace package、包级 re-export 和文件删除增加测试；
- [ ] watcher 增加 debounce、错误退避及连续变更合并；
- [ ] 明确检测无法安全替换的扩展模块并要求进程重启。

验收标准：静态与显式动态依赖均能触发正确 Entry；失败替换后旧版本继续服务；不相关模块不被移出缓存。

### P2：类型、可观测性和资源治理

- [ ] 提供基于 `Protocol`/泛型的服务键类型方案；
- [ ] 增加状态转换耗时、加载次数、回滚次数和活跃任务指标；
- [ ] 为错误历史、已移除 Entry Realm 和无监听器事件桶提供回收；
- [ ] 增加 broker/multiplexer 参考实现，支持滚动切流和多提供者；
- [ ] 增加受控进程插件适配层，用于不可信或不可逆插件。

## 11. 测试与验证

```bash
cd python
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python -m unittest discover -v
ruff check src tests examples
ruff format --check src tests examples
PYTHONPATH=src python examples/graceful_unload.py
PYTHONPATH=src python examples/dsh_like.py
DEEPSEEK_API_KEY=... PYTHONPATH=src python -m examples.cordis_agent
```

当前测试覆盖 Effect LIFO、部分初始化回滚、异步惯性、依赖出现与消失、卸载顺序、隔离和拦截、嵌套插件、配置回滚、Loader 增量协调，以及 HMR 成功、传递依赖和失败回滚。

## 12. 参考

- [论文：A Programming Paradigm for Spatiotemporal Composability](https://arxiv.org/abs/2608.25512)
- [Cordis Python / Cordis Agent 与最新 DSH 的差距](cordis-agent-vs-dsh.md)
- [Python 包入口与快速开始](../README.md)
- [DSH-like Agent Harness Demo](../examples/dsh_like.py)
- [Cordis Agent（真实 DeepSeek API）](../examples/cordis_agent/)
- [完整卸载 Demo](../examples/graceful_unload.py)
