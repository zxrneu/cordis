# Python 实现与维护参考

这份文档供阅读源码、排查生命周期问题和维护运行时使用。第一次接触项目请从[中文入门指南](zh-CN.md)开始；方法签名见 [API 参考](api.md)，公开概念见[核心概念](core-concepts.md)，类型用法见[类型化插件开发](typing.md)。

以下内容针对本仓库 `python/src/cordis` 的实现。上游 TypeScript 包的更新不会自动应用到 Python 版本。

## 运行时内部

### 代码结构

| 文件 | 职责 |
| --- | --- |
| [core.py](../src/cordis/core.py) | Component/BoundComponent、Fiber/FiberView、Effect、Context、服务、Realm、通知与查询 |
| [keys.py](../src/cordis/keys.py) | ServiceKey 与服务描述符；EventKey、QueryKey 的类型契约与身份 |
| [loader.py](../src/cordis/loader.py) | 类型化挂载、动态 Entry 清单协调、嵌套组、配置文件监听 |
| [hmr.py](../src/cordis/hmr.py) | Python 模块依赖图、过期 Entry 检测、模块替换与恢复 |
| [config.py](../src/cordis/config.py) | 不可变 EntryTree、存储/编码协议、事务 ConfigStore 与多文件 include |
| [services.py](../src/cordis/services.py) | 可选 Service 基类、BoundService 与调用方 Context 传播 |
| [accessors.py](../src/cordis/accessors.py) | 生命周期绑定的 Context accessor 描述 |
| [logger.py](../src/cordis/logger.py) / [timer.py](../src/cordis/timer.py) | 基于标准库和受管 Task 的辅助层 |
| [registry.py](../src/cordis/registry.py) / [schema.py](../src/cordis/schema.py) | 组件检查操作与配置 Schema 适配边界 |
| [import_tracing.py](../src/cordis/import_tracing.py) | 插件导入窗口内的动态 import 边采集 |
| [__init__.py](../src/cordis/__init__.py) | 公共 API |
| [tests/](../tests/) | 生命周期、失败回滚、隔离、Loader 与 HMR 测试 |

实现只要求 Python 3.11+，核心没有第三方依赖。YAML 配置需要可选的 PyYAML。

### 类型信息经过哪些边界

`Component.configure()` 返回不可变 BoundComponent；Loader.mount 在配置与插件仍关联时调用 ctx.use，返回类型化 Fiber，然后只将 FiberView 放进 `_mounted`。旧 Entry 在 `_loaded` 中保留动态插件与配置，用于文件清单和 HMR；两者共享 id 范围，但 reconcile 只协调 `_loaded`。

Context 的 fiber/fibers/errors 及 Loader.get/reload 只暴露 FiberView 的只读属性和配置无关的生命周期操作。视图保留 Context 类型，不提供 update；不要把异构 Fiber 声明为 Fiber[object, Context]，否则会虚假允许任意配置写入。Fiber 和 Loader 的 ctx 属性只读，防止通过更宽的视图替换实际 Context。

Component 和 BoundComponent 的 Context 参数逆变：基础 Context 插件可以挂到子类 Context。配置类型不变；EventKey、QueryKey 的类型参数也不变，防止检查器通过拓宽类型接受错误参数。`decode(raw, decoder)` 在绑定前转换原始数据，validate 继续处理 ConfigT，二者不能用 cast 代替。

`_Runtime.listeners` 使用字符串或 EventKey，`queries` 使用 QueryKey 对应单个 `_Listener`。事件键按对象身份匹配，同名的不同键不共享注册。handle/query 在入口关联类型，仅在查询结果离开异构注册表时恢复 ResultT；两者复用 `_invoke`，回调在注册方的 Sink 中执行。操作指南见[迁移说明](migration-typed-interfaces.md)。

### Effect 执行器

内部 `_execute()` 把不同 Python 形态归一化：

- 普通返回值：`None` 或 cleanup callable；
- coroutine：等待后处理其返回值；
- generator / async generator：逐项登记 cleanup；
- context manager / async context manager：进入后登记退出操作；
- 嵌套 `ctx.effect()`：作为父 Effect 的一个逆操作。

每次迭代前会检查 guard。组件目标发生变化时，不再启动下一步；当前已经开始的异步步骤遵循惯性语义，运行完成后立刻回滚。

### Fiber 目标与提交视图

Fiber 的 target 由两部分组成：

```text
(配置 revision, 每个声明依赖的 provider UID)
```

target 变化意味着配置或依赖身份变化。Fiber 会经过 `UNLOADING`，撤销旧 Effects，再根据最新 target 决定重新加载还是等待。

加载开始时，Fiber 将依赖解析结果复制进 `committed`。直到它的 cleanup 全部完成才清空，因此 teardown 看到的依赖不会在中途漂移。

### 失败模型

- 初始化失败：撤销已经登记的 Effect，Fiber 进入 `FAILED`；
- cleanup 失败：其余 cleanup 继续执行，最终以 `ExceptionGroup` 汇总；
- `restart()`：清除失败结果并增加 revision；
- `update()`：新配置加载失败时，默认尝试重新加载上一个有效配置；自定义原地更新 hook 需要自行管理回滚；
- 一个 Fiber 失败不会自动终止兄弟 Fiber。

可以监听内部事件或读取快照：

```python
root.on("internal/status", lambda fiber, old: print(old, "->", fiber.state))
root.on("internal/error", lambda error, fiber: print(fiber, error))

print(root.fibers)
print(root.errors)
```

## 任务归属与卸载顺序

`ctx.spawn()` 把异步工作登记到当前 Fiber 或嵌套 Effect。运行时先关闭该作用域的新任务入口，等待所有在途任务结束，然后才按 LIFO 顺序执行资源 cleanup。提供者会等待依赖它的消费者完成这个过程。

例如，[CSV 提供方](../examples/order_importer/providers/csv.py)的 `submit()` 方法把任务登记到导入插件。下面是该方法的节选，类型和所属类见源文件：

```python
def submit(self, request: ImportRequest) -> asyncio.Task[ImportReport]:
    return self.ctx.spawn(
        self._import(request),
        policy="drain", timeout=5, name=f"import:{request.path.name}",
    )
```

`spawn(awaitable, *, policy="drain", timeout=None, name=None)` 返回普通 `asyncio.Task`，可直接等待结果或调用 `cancel()`：

- `drain`：卸载时等待任务自然完成，默认没有期限；
- `cancel`：卸载时立即请求取消；
- `drain` 配合 `timeout`：卸载后等待指定秒数，超时则请求取消；这是排空期限，不是任务总执行时限。

三种方式都会等待协作取消及 `finally` 完成后才释放依赖。不同任务同时开始排空或取消，避免一个 drain 阻塞另一个任务的取消。任务失败带着所属 Fiber 写入 `ctx.errors` 并触发 `internal/error`；普通取消不视为运行时错误。`fiber.tasks` 返回未完成任务快照，已完成任务自动移出注册表。`ctx.flush()` 仍只等待生命周期稳定，不等待后台任务全部结束。

在 `ctx.effect()` 内启动的任务、异步通知和查询处理器也归属于该 Effect，清理整个 Effect 时会先排空这些任务。单独清理 on/handle 返回的句柄只移除注册，已触发任务仍归注册作用域管理。开始卸载后，旧作用域不能创建新任务、服务、监听器或子插件。

运行时无法反向发现裸 `asyncio.create_task()` 的服务引用，这些任务仍需插件自己管理。受管任务不能等待自身、所属 Effect、父插件或依赖服务的卸载／重启；这种调用会导致自等待，运行时会抛出 `InactiveContextError`。应把重载请求交给作用域之外的宿主执行。

## Python 代码热重载

HMR 的流程是：

1. 从已加载 Python 源模块构建 AST 静态 import 图，并合并插件加载期捕获的动态 import 边；
2. 从变更文件沿反向依赖找到受影响模块；
3. 找出插件模块受影响的 Loader Entry；
4. 备份并清除相应的 `sys.modules` 和字节码缓存；
5. 导入新模块，卸载旧 Fiber，启动新 Fiber；
6. 导入失败时恢复模块缓存；替换启动失败时，还会尝试用旧 Component 重建 Fiber。

```python
reloaded = await loader.reload_changed(
    ["myapp/shared.py"],
    externals={"native_extension", "process_singleton"},
)
```

变更跨越 `externals` 边界时抛出 `FullRestartRequired`，调用者应执行进程级重启。

源码 HMR 仅追踪动态 Entry 的字符串定位符；类型化 mount 通过 reload(id) 重启原组件定义。EventKey/QueryKey 依赖共享对象身份，契约模块被重新导入会生成新键；宿主应让契约模块保持稳定，或同步替换引用它的参与方。

HMR 的恢复范围是模块缓存和受管插件，恢复本身也可能失败。模块 import 阶段写文件、发网络请求或修改第三方全局状态时，恢复 `sys.modules` 无法撤销这些外部变化。插件模块应尽量保持模块顶层不产生外部副作用，把资源创建放进 Component 并登记 cleanup。

## 当前限制与风险

这里记录当前 Python 实现的限制。它适合受控试点；生产使用需要针对目标业务补充恢复、容量和持续运行验证。

2026-09-10 的[bug 审计](bug-audit-2026-09-10.md)确认的 10 项缺陷已修复；当前行为和回归证据见[修复记录](bug-fixes-2026-09-13.md)。

| 优先级 | 瑕疵 | 影响 / 临时做法 |
| --- | --- | --- |
| 边界 | 裸 Task、线程及忽略取消的协程不会被强制终止 | 使用 `ctx.spawn()` 纳管协作式协程；其他资源由插件管理 |
| P0 | cleanup 没有统一超时 | 业务 cleanup 自己使用 `asyncio.timeout()` |
| 边界 | Loader 更新有预检和失败恢复，不提供数据库式隔离 | 失败时恢复受影响的旧 Entry/挂载；恢复本身失败会聚合报告；外部副作用需应用补偿 |
| 边界 | 同一 Loader 的管理操作串行执行 | 不同 Loader、直接 Fiber 操作及进程外更新仍需宿主协调 |
| 边界 | reconcile 成功不等于业务就绪 | ACTIVE 父 Context 下传播本轮启动失败；缺少依赖仍可 PENDING；父级 LOADING 时子激活延后 |
| P1 | HMR 动态依赖只在插件导入窗口采集 | 插件启动后才发生的条件导入、运行时路径修改和部分 namespace package 仍需显式 reload 或完整重启 |
| P1 | HMR 恢复模块缓存、父包绑定和 Component，不撤销 import-time 外部副作用 | 保持模块顶层纯净，把副作用放进插件生命周期 |
| P1 | 没有依赖环诊断 | 循环依赖会保持 `PENDING`；通过状态事件和 `fibers` 排查 |
| 边界 | 字符串服务名、字符串事件和 Entry 文件清单仍是动态接口 | 类型化代码使用服务键、事件键及 configure/decode/mount；这些入口不会自动验证旧动态代码 |
| 边界 | mount 与 reconcile 管理不同集合 | mount 不进入 entries，不被 reconcile([]) 删除，也不参与源码 HMR；统一清理用 close |
| P2 | 每个 Realm 只允许一个直接提供者 | 需要多实例时增加 broker/multiplexer 服务 |
| P2 | `watch_file()` / `HotReloader.watch()` 只有轮询，没有 debounce、退避和错误恢复策略 | 由进程入口监督 watcher |
| P2 | 任务创建没有内置容量限制，Realm 缓存和错误历史没有回收策略 | 需要宿主限制并发，并为长期运行设置容量和观测策略 |
| P2 | ModuleGraph 扫描已加载模块并读取源码 | 大型进程中 HMR 检测延迟会随模块数增长 |
| 边界 | 任意全局变量、泄漏线程、外部进程和不可逆 emission 不受 Context 控制 | 使用进程隔离、事务、幂等和补偿操作 |

## 维护清单

### 已完成：任务所有权与协作式卸载

- [x] 增加 `ctx.spawn(awaitable, *, policy="drain", timeout=None)`；
- [x] Task 自动继承当前 Fiber 和嵌套 Effect、收集异常并在 unload 时停止接收；
- [x] 支持 `drain`、`cancel` 和超时后 cancel；
- [x] 让异步事件回调自动使用监听器所属 Fiber 的任务域；
- [x] 添加“提供者卸载时，在途消费者完成后才关闭服务”的竞态测试。

验证结果：受管任务无需手动维护 Task set，协作式任务在卸载完成前全部结束；超时后请求取消并等待 `finally`；任务异常进入 `internal/error`。取消不响应、阻塞事件循环及通用 cleanup 超时仍属于边界。

### P1：配置事务和诊断

- [x] mutation 前完成启用候选导入、Entry 结构校验和顶层变更配置校验；
- [ ] 完整的跨条目 Realm 冲突预检；
- [x] reconcile 失败时恢复旧 Entry/Fiber 集合；
- [x] 区分期望配置与已生效配置，修复回滚后再次提交同一配置被跳过的问题；
- [x] 串行协调配置更新、卸载与代码重载，补充并发操作测试；
- [x] reconcile 传播本轮启动失败（包括组内和依赖变更的消费者），保留 PENDING 状态语义；
- [ ] 提供整组业务就绪等待与超时诊断；
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

- [x] 提供 `ServiceKey[T]`、Protocol/Context 描述符和泛型插件配置；
- [x] 用 FiberView 分离检查与配置写入，保留动态查询的 Context 类型；
- [x] 增加 EventKey/QueryKey 与类型化通知、单处理器查询；
- [x] 用 BoundComponent 和 configure/decode/mount 保留配置绑定关系；
- [x] 启用 mypy strict，验证正确用法的推断结果和错误用法的拒绝行为；
- [ ] 增加状态转换耗时、加载次数、回滚次数和活跃任务指标；
- [ ] 为错误历史、已移除 Entry Realm 和无监听器事件桶提供回收；
- [ ] 增加 broker/multiplexer 参考实现，支持滚动切流和多提供者；
- [ ] 增加受控进程插件适配层，用于不可信或不可逆插件。

## 其他应用示例

- [安全卸载](../examples/graceful_unload.py)：观察在途任务结束、消费者退出和提供者释放的顺序。
- [离线 Agent](../examples/dsh_like.py)：把模型适配、工具、策略、会话和 Agent Loop 拆成插件，无需网络或 API Key。
- [Cordis Agent](../examples/cordis_agent/)：包含模型客户端、文件工具和会话持久化的应用示例；命令参数可通过 `uv run python -m examples.cordis_agent --help` 查看。连接模型服务需要单独配置凭据；插件代码本身不受安全沙箱隔离。
- [与 DSH 的架构比较](cordis-agent-vs-dsh.md)：讨论 Agent 应用层的能力和差距。

## 本地验证

从仓库根目录执行：

```bash
cd python
uv sync
uv run mypy
uv run python -m unittest discover -v
uv run ruff check src tests examples typing_tests
uv run python -m examples.graceful_unload
uv run python -m examples.typed_interfaces
uv run python -m examples.order_importer --demo
```

测试覆盖生命周期清理、失败恢复、服务依赖、任务归属、Loader、HMR、类型契约和配置驱动的订单导入。它们不能替代目标业务的负载测试、长期运行测试及故障注入。

## 设计背景

论文 [A Programming Paradigm for Spatiotemporal Composability](https://arxiv.org/abs/2608.25512) 描述了相关设计。Python 版本围绕生命周期清理与响应式依赖实现这些机制；具体行为和边界以本仓库代码、测试及上述限制为准。
