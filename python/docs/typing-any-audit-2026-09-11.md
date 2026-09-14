# Any 使用审计（2026-09-11）

后续运行时修复见 [2026-09-13 修复记录](bug-fixes-2026-09-13.md)。本文测试数量和审计结论保留为当日记录。

**已收紧本轮确认的 3 类公开类型出口，并落实类型化接口调整。** 除了阻止 Any 泄漏，还用 FiberView 分离配置写入权限，增加类型化通知与查询键，并通过绑定配置保留 Loader 的类型关联。

初始审计基线为 `adfd5fe`，使用仓库现有 CPython 3.11.11 / mypy 环境。后续按用户要求修改了 core.py、loader.py，并补充类型契约和回归测试；[上一轮运行时审计](bug-audit-2026-09-10.md)的 8 个生命周期 bug 不属于本次修复范围。

## 修复结果

| 项目 | 当前行为 |
| --- | --- |
| T01 | `ctx.fiber.ctx` 保留 Self；Fiber 快照、Loader.get/reload 和错误归属中的 ctx 至少为 Context；经这些路径访问 ServiceKey 仍检查参数。 |
| T02 | serial、bail、waterfall 及内部派发结果为 object，调用方使用前收窄。 |
| T03 | Entry.intercept 和冻结映射函数使用 `Mapping[str, object]`；核心 metadata 接口与 Loader 保持一致。 |
| 内部收紧 | 任务集合使用 Task[object]，Effect 迭代器使用同步/异步迭代器联合类型；服务适配器的存储值、元数据、结果为 object，仅在注册边界恢复 T。 |
| 精确类型 | `ctx.use()` 的 Fiber[ConfigT, ContextT]、ServiceKey[T] 和单次 spawn 的 Task[T] 保持原有类型精度。 |
| FiberView | Context、列表、错误归属及 Loader 字符串查询返回属性只读的视图；不暴露 update，await / wait 后仍是视图。Loader 还保留 Context 子类类型。 |
| 事件契约 | EventKey[T] 关联通知与回调参数；QueryKey[RequestT, ResultT] 关联 handle/query。键按对象身份匹配，避免同名的不同类型契约混用。 |
| 配置绑定 | configure 返回 BoundComponent，Loader.mount 保留配置与 Context 类型；decode 将 object 转成 ConfigT 后再绑定，支持同步或异步 decoder。 |

验证：完整 unittest **87 项通过**；mypy 的 **25 个源码/示例/契约文件通过**；Ruff 通过。负例测试从初始 17 个检查点、首轮收紧后的 32 个，增加到 **66 个**，要求每一处都产生诊断。新增 13 个运行时测试覆盖类型化挂载、配置解码、键隔离及查询任务的归属和排空；[完整示例](../examples/typed_interfaces.py)已运行通过。

首轮把公开动态句柄标为 Fiber[object, Context] 虽然补住 Context 的 Any 泄漏，却仍会允许 update 接收任意对象。现已改为 FiberView：未知配置只供检查，配置写入必须保留 ctx.use 或 Loader.mount 的类型化返回值。视图是静态协议，不包装 Fiber，也不深度冻结配置。

原始 Entry 配置映射 `Mapping[str, Any]`、反射插件定义及字符串服务查询仍是动态边界；代码内可选择绑定入口。mount 条目由 unmount/close 管理，不进入声明式 entries；源码 HMR 仍处理 Entry 的字符串定位符。内部注册表保留必要的类型擦除，仅在匹配键和绑定入口恢复具体类型。以下保留修复前的诊断与基线代码位置，便于追溯。

## 可执行证据

[复现文件](repros/any_audit_20260911.py)的业务函数全部有类型注解，没有 Any、cast 或 type ignore。执行：

```bash
cd python
.venv/bin/python -m mypy docs/repros/any_audit_20260911.py
PYTHONPATH=src .venv/bin/python docs/repros/any_audit_20260911.py
```

修复前 mypy 报成功；修复后该文件应得到 **3 个类型错误**（退出码 1），分别对应三个 `SHOULD_REJECT` 标记。直接运行 Python 会跳过静态检查，仍输出以下结果；object 本身不会增加运行时校验：

```text
T01: annotated int, actual str: 'wrong'
T02: AttributeError: 'str' object has no attribute 'bit_length'
T03: AttributeError: 'str' object has no attribute 'bit_length'
```

对照检查：同一个 `COUNT = ServiceKey[int]("count")`，直接调用 `ctx.provide(COUNT, "wrong")` 会得到 `Cannot infer value of type parameter "T" of "provide" of "Context"`。修复前经过 `ctx.fiber.ctx` 后没有诊断；修复后两条路径均被拒绝。

## 修复前的诊断

### T01：Fiber 的 Context 被完全擦成 Any

位置：[core.py:1142](../src/cordis/core.py#L1142)、[loader.py:98](../src/cordis/loader.py#L98)。

`Context.fiber`、`Context.fibers` 和 `Loader.get()` 暴露 `Fiber[Any, Any]`，第二个参数把 `fiber.ctx` 变成了 Any。结果不仅丢失配置类型，连 Context 已有的方法签名也丢失：

```python
COUNT = ServiceKey[int]("count")
ctx.fiber.ctx.provide(COUNT, "wrong")  # mypy 通过。
value: int = ctx[COUNT]                # 实际是 str。
```

异构 Fiber 的配置类型可能不同，但这不意味着它们的 Context 连公共接口也未知。内部类型擦除不应通过这个出口扩散到使用方。

**建议**：优先让读取到的 `ctx` 至少是 Context，能保留子类时保留子类；异构集合可使用只读的 Fiber 协议/视图。不要机械替换为 `Fiber[object, Context]`：Fiber/Component 的泛型可变性以及配置更新接口仍需设计，错误的替换会拒绝合法插件或虚假宣称可接受任意配置。

**验收**：直接和经 Fiber/Loader 读取的 Context 都拒绝错误的 `ServiceKey[T]` 注册；现有 `ctx.use()` 返回的精确配置与 Context 类型保持不变。

### T02：无类型约束的事件结果使用 Any，而不是要求调用者收窄

位置：[core.py:1043](../src/cordis/core.py#L1043)、[core.py:1579](../src/cordis/core.py#L1579)、[core.py:1586](../src/cordis/core.py#L1586)、[core.py:1599](../src/cordis/core.py#L1599)。

`_invoke()`、`serial()`、`bail()`、`waterfall()` 使用 Any 返回值。任意字符串事件确实不能推导出确定结果类型，但完全可以表达为“结果未知，需要检查”。当前以下错误直接通过：

```python
ctx.on("answer", lambda: "text")
value: int = ctx.bail("answer").bit_length()
```

事件 API 的动态特性已有文档说明；这里指出的是可以收紧的类型出口，不宣称现有 API 已承诺推导事件类型。

**建议**：无类型事件返回 object，调用方先检查；需要类型推导时另提供携带参数/结果类型的事件键或协议。waterfall 要同时考虑中间件可能替换结果，不能只根据 final 的类型虚假保证整个链的返回类型。

**验收**：未知事件结果上直接访问成员必须报类型错误；经过 `isinstance` 收窄或使用有类型的事件接口后，正确用法通过。

### T03：Entry 的不透明元数据没有必要用 Any

位置：[loader.py:31](../src/cordis/loader.py#L31)、[loader.py:42](../src/cordis/loader.py#L42)、[loader.py:81](../src/cordis/loader.py#L81)。

`_freeze_mapping()` 和 `Entry.intercept` 使用 `Mapping[str, Any]`。它们只复制、保存和传递元数据，使用 object 已足以表达该职责。相比之下，核心的 `Context.metadata()` 已返回 object，Loader 却把同类信息放宽成 Any。

```python
entry = Entry("app", app, intercept={"db": "text"})
value: int = entry.intercept["db"].bit_length()  # mypy 通过，运行时报错。
```

此外，`Entry(**raw)` 接收 `Mapping[str, Any]`，也跳过了对每个配置字段的静态检查。这属于配置解析边界，应通过 TypedDict 或逐字段解析建立结构，不能把读取任意字典等同于已经验证 Entry。

**建议及验收**：不透明 metadata 改为 `Mapping[str, object]`，业务读取需要收窄；原始配置与验证后的 Entry 使用不同类型。JSON/TOML 导入处仍执行运行时校验，不能用 cast 代替。

## 内部可改进项及合理保留

| 使用位置 | 判断 |
| --- | --- |
| `_execute()` 的 `iterator: Any`，[core.py:380](../src/cordis/core.py#L380) | 可收紧。同步、异步分支可分别使用 `Iterator[object]` / `AsyncIterator[object]`，生命周期协议显式建模，使错误调用也能被检查。 |
| `_Service.adapt` / `_adapt()` | 服务存储确实是异构边界，但 value、metadata 和结果应尽量保留 object；需要断言类型时集中在通过 ServiceKey 恢复泛型的入口。 |
| `_as_component()`、反射 import | 动态插件发现允许小范围类型擦除，应在完成运行时形状检查后进入类型明确的内部接口。 |
| `ServiceKey[Any]` 用于异构依赖名集合 | 有合理性：此处只消费名称，不代表读取服务的值类型；不能直接改为不变的 `ServiceKey[object]`。也可考虑只暴露 name 的协议。 |
| 插件注册表中的 `Component[Any, Any]`、重载实现签名 | 有合理性：不同配置、不同 Context 子类要共同存储；仅有实现签名中的 Any 不代表调用者已失去类型，需看公开 overload。 |
| `Task[Any]` 任务集合 | 可以表达不同返回类型的任务；若只是取消/等待而不消费结果，可以尝试 object 或生命周期协议，没必要为了消除计数而增加错误断言。 |
| 字符串服务读取、动态 `__getattr__` | 当前明确选择的动态 API。保留兼容性时不能一概算误用；需要类型保证的业务应使用服务键或描述符。 |

## 类型检查的盲区

[现有配置](../pyproject.toml)启用了 mypy strict，但它仍允许显式 Any 及其传播。例如，把一个 Any 表达式赋给声明为 int 的局部变量，再返回这个变量，并不会产生本轮期待的诊断。

[负例测试](../typing_tests/invalid.py)原先仅覆盖直接服务键、配置和任务调用，现已补齐 `ctx.fiber.ctx`、`Loader.get().ctx`、事件返回值及 Entry 元数据等间接出口，并通过[正例契约](../typing_tests/contracts.py)验证精确类型和收窄后的合法用法。

这 3 类出口和相应负例已完成收紧。后续可在选定的业务模块中试用 `disallow_any_expr` / `disallow_any_explicit` 辅助审计；不要一次对整个动态运行时强开后用大量 ignore 消音。
