# Cordis Python / Cordis Agent 与最新 DeepSeek Harness 的差距

> 比较日期：2026-09-03  
> DSH 基线：官方最新预发布版 [`v0.1.2-alpha.5`](https://github.com/deepseek-ai/deepseek-harness/releases/tag/dsh-v0.1.2-alpha.5)，以及同日 `master` 的 [`49a606b`](https://github.com/deepseek-ai/deepseek-harness/commit/49a606bc5b5934603f22a26957a07dc799ab0291)。  
> 本项目基线：`src/cordis/` 的 Python Cordis 运行时，加上 `examples/cordis_agent/` 的真实 DeepSeek API Demo。

> Python 运行时更新（2026-09-09）：已加入 `ctx.spawn()`、嵌套 Effect 任务归属、异步事件任务归属和协作式卸载。下文相应条目已更新；DSH 一侧仍使用上面的 2026-09-03 比较基线。

## 1. 结论

当前实现是一个**能真实运行的 Python Agent Harness 纵向切片**，不是 DSH 的 Python 复刻，也还不能替代 DSH。

已经成立的部分是架构骨架：模型、工具、会话、策略、提示词、Agent Loop 和入口都能做成插件；插件有依赖、有可逆卸载；工具能由模型真实调用；会话能写入 JSONL 并恢复。这足以证明“Everything is a Plugin”在 Python 中可以落地。

主要不足不在插件数量，而在四个运行时闭环仍未形成：

1. **执行闭环**：运行时已有任务归属、协作取消和卸载排空；Agent 产品仍缺少单工具超时、并行调度及工具调用 lease 协议。
2. **安全闭环**：路径检查不是进程沙箱；审批、权限预设、沙箱执行和审计日志没有连成一条强制执行链。
3. **会话闭环**：保存的是 API message，不是结构化事件源，无法可靠表达流式 chunk、审批、取消、压缩、子 Agent、回放和投影。
4. **上下文闭环**：没有 token 计量、上下文组装、溢出恢复和压缩，长会话最终会超过模型窗口。

如果目标只是“可读、可改少量文件的本地 Python Agent”，当前 Demo 已可用；如果目标是“接近 DSH 的生产级 Harness”，仍缺少大部分产品运行时。

## 2. 比较口径

DSH 不是一个单独的 Agent Loop。官方架构把模型适配器、工具注册表、会话日志和 Agent Loop 都实现为 Cordis 插件，再由 bundle、profile 与有序配置 patch 组成具体产品；官方提供 Web、Headless、SDK、SDK Minimal 和 ACP 等 profile。[DSH 架构文档](https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/architecture.md)

本项目也分两层：

- `src/cordis/`：通用 Python 插件运行时，负责 Fiber、Effect、依赖注入、Loader 与 HMR；
- `examples/cordis_agent/`：基于这个运行时组装的最小 Agent 产品。

因此下文同时比较“Cordis 运行时能力”和“Agent 产品能力”。不能把 DSH 的 Python SDK 当成 Python 原生 Harness：官方 SDK profile 启动的仍是正常的 DSH 运行时，Python 包是它的客户端接入面，而本项目的运行时本身就是 Python。

状态含义：

- **有**：主链路已经实现且有测试或可运行 Demo；
- **部分**：存在最小实现，但缺少 DSH 的关键语义；
- **无**：当前仓库没有相应能力。

## 3. 总览矩阵

| 能力 | 当前实现 | DSH `0.1.2-alpha.5` | 差距与后果 |
| --- | --- | --- | --- |
| Everything is a Plugin | 有 | 有 | 核心理念对齐；当前 bundle 仍硬编码在 Python 函数中 |
| 可逆插件生命周期 | 有 | 有 | Python 已支持 LIFO cleanup、依赖驱动停启和失败回滚 |
| 配置组合 | 部分 | bundle + profile + 多层 patch | 当前只有 `Entry` 列表和 Loader，没有产品级 profile、schema 化 patch 与配置来源优先级 |
| Live patch / HMR | 部分 | 按 profile 决定是否 live apply | Python 有模块 HMR，但依赖静态 AST import，且不能撤销 import-time 外部副作用 |
| Agent 生命周期 | 部分 | Agent handle、投递队列、状态、取消、拦截、作用域 | 当前只有一次 `run(text)`；没有 inbox、排队输入、取消、状态事件或维护阶段 |
| Agent / 工具作用域 | 无 | 注册可按 Agent 可见并由 scope 拥有 | 当前一个 Context 内的工具对所有调用者全局可见 |
| LLM 适配层 | 部分 | 可注册 provider，统一流式协议 | 当前只实现 DeepSeek Chat Completions 的单一非流式适配器 |
| 流式输出与 reasoning | 无/部分 | typed content block、raw chunk、组装器 | reasoning 只随最终 message 被动保存；无增量事件、UI 回放或中途取消 |
| Token 用量与计量 | 无 | 每次调用用量、缓存 token、重放计量 | 无成本、窗口压力和压缩决策依据 |
| Context 组装与压缩 | 无 | System Prompt 组装、token meter、compaction | 长会话只能持续累加，最终上下文溢出 |
| Tool schema / 注册 | 有 | 有，且支持作用域、host metadata、presenter | 当前 schema 是普通 dict，没有统一校验、显示协议和输出 schema |
| Tool 执行管线 | 部分 | pre/execute/post waterfall、结果事件 | 当前是一次字典查找后直接调用，策略只能写死在执行器中 |
| 多 ToolCall | 有 | 有 | 当前同一回答的多个调用按顺序执行 |
| 并行工具调用 | 无 | 对并发安全工具可并行 | 独立慢工具也会串行，延迟累加 |
| 工具超时与取消 | 无 | `timeoutMs`、取消信号、结果分类 | 卡住的工具会卡住整个 turn，也会妨碍插件卸载 |
| 在途任务归属 | 部分 | Agent/Scope/Fiber 生命周期协同 | `ctx.spawn()` 和异步事件已跟随 Fiber/Effect；裸 Task 和 Agent 工具调用 lease 仍需接入 |
| Session 事件源 | 部分 | typed append-only `SessionEvent` 是唯一事实源 | 当前 JSONL 只存 API message，非消息状态无法可靠回放 |
| 持久化与崩溃恢复 | 部分 | 独立 persistence seam、flush checkpoint、修复与版本头 | 当前逐行 append，无锁、无校验、无原子 checkpoint、尾行损坏恢复或格式迁移 |
| Session 投影 / 查询 | 无 | 从事件导出消息、标题、列表、遥测等投影 | 当前直接把持久化数据当模型 messages，存储模型与展示模型耦合 |
| Fork / lineage / reference | 无 | 会话分叉、谱系、跨会话引用 | 无法表达基于某个边界分叉或引用另一会话 |
| 审批 | 部分 | 独立服务、应答 waterfall、每会话策略、审计事件 | 当前只有修改型工具的终端 `y/N`，没有请求 ID、审计、策略切换或 UI/ACP 应答者 |
| 真实沙箱 | 无 | macOS Seatbelt、Linux bwrap/Landlock、Windows 受限令牌/ACL | 当前只保护自带文件工具；插件、网络、子进程和 Python 代码仍有进程权限 |
| 权限预设 | 无 | `read-only` / `workspace-write` / `danger-full-access` 与审批组合 | 无会话级权限状态、升级重试与可回放策略 |
| Shell / 子进程 / PTY | 无 | 前台命令、持久 PTY、超时、截断、spill | 还不是通用编程 Agent |
| 后台 Job | 无 | 可观察、收集和终止后台任务 | 无长任务管理与结果回收 |
| 文件编辑能力 | 部分 | 文件系统 seam、读写编辑结果与观测状态 | 只有整文件覆盖；无 patch、diff、冲突检测、二进制/图片能力 |
| LSP / Code Runtime / Web | 无 | 都是可选能力插件 | 缺少符号导航、结构化代码执行、搜索与抓取 |
| Skills / Workflows | 无 | provider registry、发现/缓存/失效、工作流运行记录 | 工具只能在代码中注册，不能按需发现说明与编排复用流程 |
| Subagents | 无 | 多 provider、前后台运行、持续子 Agent、消息与中断 | 无隔离上下文、委派、并行协作和父子生命周期 |
| Goals / Schedule / Plan | 无 | 可持久状态和相应事件 | 无长任务目标、提醒和计划态 |
| Settings / Credentials | 无 | 分层设置与凭据引用/按操作解析 | API Key 只来自参数或环境变量；无脱敏元数据、热更新与多来源解析 |
| 产品接入面 | Headless CLI | Web、Headless、SDK、SDK Minimal、ACP、HTTP | 无服务器协议、浏览器 UI、远程客户端与标准 Agent 接入 |
| 可观测性 | 部分 | 会话遥测、usage、运行事件、客户端投影 | 当前只有 Fiber 状态/错误快照和 stdout |

DSH 的工具执行契约明确包含每工具超时、并发安全标记、取消信号和 `tools/pre-execute`、`tools/execute`、`tools/post-execute` 策略链；当前 Demo 的 `ToolRuntime.execute()` 还没有这些扩展点。[DSH Tools 参考](https://deepseek-harness.github.io/deepseek-harness/en/reference/subsystems/tools)

## 4. 已经对齐的部分

### 4.1 插件边界是正确的

`session_plugin`、`policy_plugin`、`tools_plugin`、`llm_plugin`、`agent_plugin` 和 `headless_plugin` 都通过服务依赖组合。替换 LLM 或工具不要求修改 Agent Loop；工具注册返回 unregister cleanup，插件卸载后注册项会被移除。

这与 DSH“核心部件也没有特权、都通过服务 seam 组合”的方向一致。它是当前项目最有价值的部分，不应该为了追求功能数量而改回一个巨型 Agent 类。

### 4.2 Python Cordis 生命周期比普通插件注册表更完整

当前运行时已有：

- Effect 逆操作按 LIFO 执行；
- 初始化中途失败会回滚已登记 Effect；
- provider 消失时 consumer 自动卸载，并在 teardown 期间保留 committed dependency view；
- 子插件归属父插件；
- 配置失败可恢复最后一次有效配置；
- 隔离 Realm、依赖 metadata 拦截和模块 HMR。

这些能力解决了“插件怎么安全装上和撤下”的基础问题。它们不是 DSH 完整能力的替代品，但为后续服务化拆分提供了可用底座。

### 4.3 Demo 确实走真实模型和真实工具协议

`DeepSeekAPI` 发出真实 `/chat/completions` 请求；`AgentRuntime` 解析模型返回的 `tool_calls`，执行工具，再将 `role=tool` 的结果送回模型。它不是用正则假装调用工具，也不是只能展示架构的 Fake LLM。

## 5. 关键不足

### 5.1 P0：工具调用仍需接入任务作用域

运行时已提供 `ctx.spawn()`，异步事件回调也会自动归属于注册者的 Fiber/Effect。裸 `asyncio.create_task()` 和直接取得 handler 引用后执行的工具调用仍不自动归属于工具提供者。卸载工具插件时仍可能发生：

```text
ToolCall 已取得 handler 引用
        |
        +-- 插件开始卸载并注销工具
        |
        +-- handler 仍在运行，继续访问即将关闭的服务
        |
        +-- provider cleanup 先关闭资源 -> 在途调用失败或写入半完成
```

当前 Demo 的工具调用由 Agent Loop 直接 `await`，`AgentRuntime.run()` 也用异步锁避免两条 turn 交错。后续需要将 Agent 执行和工具调用 lease 接入运行时任务域；异步锁不等价于带投递、状态和取消语义的 Agent inbox。后台工作现在可以这样登记：

```python
task = ctx.spawn(coro, policy="drain", timeout=30)
```

运行时已实现停止接收新任务、记录所属 Fiber、消费异常、卸载时 drain 或 cancel、超时后请求取消，以及 provider 等待依赖 consumer 的受管任务结束后再 cleanup。取消是协作式的，忽略取消或阻塞事件循环的代码仍无法强制终止。

### 5.2 P0：路径限制不是安全沙箱

`Workspace.resolve()` 能阻止 `../` 逃逸并拒绝 `.git`，这是文件工具自身的参数校验，不是进程级隔离：

- 第三方插件可以直接调用 `Path("/")`、`subprocess` 或 socket；
- LLM 适配器拥有网络能力，策略不能限制目标地址；
- 若以后添加 shell，shell 继承 Python 进程权限；
- 符号链接和检查后使用之间仍需要按具体操作审计；
- CPU、内存、进程数和网络不受控制。

DSH 把审批和沙箱分开：审批回答“这次操作是否允许”，沙箱回答“即使允许，进程实际能写到哪里”。其本地后端按平台实施文件效果隔离，并在每次能力调用时解析 mode 与 workspace root。[DSH Sandbox 参考](https://deepseek-harness.github.io/deepseek-harness/en/reference/subsystems/sandbox) [DSH Approval 参考](https://deepseek-harness.github.io/deepseek-harness/en/reference/subsystems/approval)

因此，在真实沙箱落地前，不应给当前 Demo 增加任意 shell 工具并称其“安全”。

### 5.3 P0：Session 不是事件源

当前 `SessionLog` 的事实源就是 `messages: list[dict]`，磁盘每行也只是：

```json
{"type":"message","message":{"role":"user","content":"..."}}
```

它能满足最小恢复，却无法无损表达：turn/step 边界、流式 chunk、请求 header、取消原因、工具执行状态、审批问答、权限切换、压缩范围、用量、子 Agent、后台 Job 和错误恢复。

DSH 使用带连续序号的 typed append-only `SessionEvent` 作为唯一事实源，模型 message 是从日志投影出来的，而不是另外维护一份可漂移的真相；持久化再处理 flush、恢复和元数据头。[DSH Sessions 参考](https://deepseek-harness.github.io/deepseek-harness/en/reference/subsystems/session) [DSH Persistence 参考](https://deepseek-harness.github.io/deepseek-harness/en/reference/subsystems/persistence)

当前设计的具体风险：

- 进程在写一半时可能留下损坏尾行，`--resume` 会整体失败；
- 同一个文件被两个进程写入时无所有权或锁；
- 没有 schema/version，升级后兼容策略不明确；
- 无法区分调用开始后崩溃与调用从未开始；
- UI、模型上下文和审计只能争用同一份原始 message。

### 5.4 P0：LLM 调用不可流式，取消也尚未贯穿整个 Harness

`DeepSeekAPI.complete()` 已使用 `asyncio.open_connection()` 进行异步 TCP/TLS 请求，timeout 或取消会关闭连接，不再用工作线程运行 `urllib`。但它仍会缓冲完整 JSON 响应，没有请求增量 stream；取消信号也没有贯穿 Agent、Tool 和 Session 生命周期，因此还不是 Harness 级协作取消。

缺少的语义包括：

- 标准化 text/reasoning/tool-call chunk；
- 首 token、结束、错误、取消事件；
- provider/model 路由与 fallback；
- 限流和可分类重试；
- token usage、cache read/write token；
- 流中断后的日志闭合和恢复；
- 多模态 content block。

DSH 的 LLM seam 统一 message/content block、流式 chunk、adapter 和 assembler，并对调用用量作统一计数。[DSH LLM Streaming 参考](https://deepseek-harness.github.io/deepseek-harness/en/reference/subsystems/llm-streaming)

### 5.5 P0：没有上下文窗口治理

当前每轮都把 `session.messages` 全量发送。只要对话足够长，必然出现上下文超限；大文件工具结果还会加速这个过程。

DSH 的 compaction 是独立能力 seam，会记录压缩开始、摘要、替换范围、被遮蔽事件和结束状态；压缩失败或中途崩溃也能从日志识别，工具调用与结果配对也会被保护。[DSH Compaction 参考](https://deepseek-harness.github.io/deepseek-harness/en/reference/subsystems/compaction)

当前至少需要：token estimator、工具结果裁剪、保留尾部策略、摘要替换事件、上下文溢出后一次受控压缩重试。简单删除旧 message 会破坏 tool-call/tool-result 配对，也无法审计模型究竟看到了什么。

### 5.6 P1：工具层只是注册表，还不是执行平台

当前工具定义只有名称、说明、参数、handler 和 `mutating` 布尔值。还缺：

- 输入 schema 的运行时校验与稳定错误码；
- canonical output 与独立 UI presenter；
- timeout、deadline、cancel signal；
- `isConcurrencySafe` 与并行批调度；
- pre/execute/post hook，用于策略、审计、缓存和观测；
- 调用开始/输出/结束的 Session 事件；
- 大输出截断与 spill 文件；
- 每 Agent/每 Session 的作用域过滤；
- 插件开始卸载后拒绝新调用，并等待已获得 lease 的调用结束。

最后一项是卸载正确性的关键。建议工具查找返回短生命周期 `lease`，执行结束释放；注销先从可发现集合移除，再等待 lease 归零，最后允许插件 cleanup。

### 5.7 P1：缺少产品级组合与配置协议

当前 `bundle(args)` 是一个 Python 函数，配置来自 argparse。它清晰但无法表达 DSH 的组合层次：基础 bundle、profile patch、用户目录 patch、命令行 patch，也没有统一 schema、来源追踪和敏感配置引用。

需要区分：

- **composition**：发布者决定产品包含哪些能力；
- **profile**：Web、Headless、SDK 等运行形态的差异；
- **settings**：用户可修改的普通设置；
- **credentials**：只保存引用，执行时解析秘密；
- **session policy**：会话内可持久化、可回放的权限与行为覆盖。

否则继续向 `argparse.Namespace` 加字段，最终会让插件表面独立、配置实际上仍被入口强耦合。

### 5.8 P1：能力生态尚未建立

对编程 Agent，以下不是装饰功能，而是完成真实任务的基础设施：

- shell/subprocess、PTY、后台 Job；
- read/write/edit/patch/diff、文件观测和大输出 spill；
- LSP 符号导航；
- Web search/fetch；
- Skills 的发现、缓存、失效和按需加载；
- Workflow 的运行身份、结果和事件；
- Subagent 的独立上下文、provider、前后台执行、消息和中断。

DSH 当前把 Skills 做成可注册 provider，并处理发现与缓存失效；Subagent 也允许多个 provider 共存，而不是把某个子 Agent 实现写死进 loop。[DSH Skills 参考](https://deepseek-harness.github.io/deepseek-harness/en/reference/subsystems/skills) [DSH Subagent 参考](https://deepseek-harness.github.io/deepseek-harness/en/reference/subsystems/subagent)

Python 实现应复制这些“能力 seam”，不必一开始复制 DSH 的所有 provider。

### 5.9 P2：还没有产品接入面

目前只有终端输入与 `--once`。缺少：

- HTTP/WebSocket 服务；
- Web UI 的会话列表、流式内容、工具展示与审批；
- SDK 的稳定请求/事件协议；
- ACP 接入；
- 客户端断线重连、游标续传和幂等投递；
- 多 workspace、多会话并发和资源配额。

这些能力不应该直接耦合 Agent Loop。先稳定 Session event 和 Agent command/event 协议，再实现不同 carrier，才能避免 CLI、Web、SDK 各自复制一套行为。

## 6. 当前实现独有的价值与取舍

差距不等于当前项目没有价值。它与 DSH 的定位也有差异：

- Python 原生插件可以直接组合现有 asyncio、数据科学和业务服务；
- 核心只依赖标准库，理解和调试成本低；
- Fiber 的可逆 Effect、依赖惯性和 committed view 已经覆盖许多普通 Python 插件框架没有处理的卸载竞态；
- 小型嵌入式 Agent 可以只带需要的能力，不必复制完整桌面产品。

应保留这些特点。目标不是逐包翻译 DSH，而是先实现一条安全、可恢复、可扩展的 Python 主链。

## 7. 建议路线图

### Milestone 0：冻结协议和基线

- [ ] 为 SessionEvent、ToolDefinition、ToolCall、ToolResult、StreamChunk 定义版本化 dataclass/Protocol；
- [ ] 把当前 Demo 的行为测试固化为协议测试；
- [ ] 明确 Python 版不追求与 DSH 内部 TypeScript 类型二进制兼容；
- [ ] 建立“模型可见内容必须能从事件日志重建”的不变式。

验收：相同事件流每次导出完全相同的模型 messages；未知事件与格式版本明确失败，而不是静默丢弃。

### Milestone 1（P0）：任务所有权与可终止卸载

- [x] 实现 `ctx.spawn()` 和 Fiber/Effect task scope；
- [x] 支持 `drain`、`cancel` 和排空超时后请求 cancel；
- [x] 异步事件自动继承注册者 scope；
- [ ] ToolCall 接入任务作用域和业务执行期限；
- [ ] 工具注册增加 lease/quiesce：先禁止新调用，再排空旧调用；
- [ ] 统一消费后台异常并写入结构化错误事件。

验收：在工具运行到一半时卸载插件，不能出现资源先关闭；协作式任务超时后取消并完成清理，测试结束没有遗留 task。忽略取消的协程需要更强的进程隔离边界。

### Milestone 2（P0）：事件溯源 Session

- [ ] 使用连续 `seq` 和版本头的 append-only event envelope；
- [ ] 记录 turn、step、user、assistant chunk、tool call/result、error、cancel；
- [ ] messages 只能由事件投影生成；
- [ ] 增加 flush checkpoint、尾行修复、单写者所有权和 crash recovery；
- [ ] 增加 resume、fork boundary 与 lineage。

验收：在任意事件边界杀死进程都能恢复到最后一个完整事实；未闭合 turn 被明确标为 interrupted；回放不会重复执行工具。

### Milestone 3（P0）：流式 LLM 与上下文治理

- [ ] 定义 provider registry 与统一 `stream()`；
- [ ] 规范 text/reasoning/tool-call/usage chunk；
- [ ] 贯穿 cancel signal、deadline、retry 分类；
- [ ] 增加 token meter、工具结果裁剪和 compaction seam；
- [ ] 记录实际请求头、provider/model、usage 和上下文替换。

验收：用户取消后网络读取和 Agent turn 都在 deadline 内结束；超长会话自动压缩后继续；日志能重建每次模型真正看到的输入。

### Milestone 4（P0/P1）：审批与真实沙箱

- [ ] 将 approval 做成独立服务和 responder chain；
- [ ] 将 sandbox mode 与 approval policy 分离并写入 Session；
- [ ] 先实现一个操作系统后端，再补平台矩阵；
- [ ] shell 和文件写入统一经过 policy → approval → sandbox → audit；
- [ ] 凭据只用引用配置，调用时解析，不进入模型日志。

验收：`read-only` 下无论模型、工具还是 shell 都不能写 workspace；`workspace-write` 不能写根目录外；每次询问与决定都有配对审计事件；沙箱不可用时 fail closed。

### Milestone 5（P1）：最小编程 Agent 工具集

- [ ] shell/subprocess，带超时、截断、退出分类和 spill；
- [ ] read/write/edit/patch，带 observed hash 与冲突检测；
- [ ] PTY 与后台 Job；
- [ ] Web fetch；
- [ ] Skills provider 和按需读取；
- [ ] 可选 LSP，避免让主链依赖某一种语言服务器。

验收：Agent 能在临时 Git 仓库内读取、修改、测试一个多文件项目；所有副作用受权限约束；大输出不会撑爆 Session。

### Milestone 6（P1/P2）：Profile、Subagent 与接入面

- [ ] bundle/profile/patch 的声明式配置与 schema 校验；
- [ ] Agent scope、inbox、排队输入、状态和取消；
- [ ] Subagent provider、前后台运行、父子取消与消息；
- [ ] HTTP 事件协议，再基于它实现 SDK 或 Web；
- [ ] telemetry、投影缓存和长会话分页。

验收：同一个 runtime 能以 headless 与 SDK profile 启动；客户端断线重连后从游标续读；父 Agent 结束后不会遗留子 Agent 或 Job。

## 8. 推荐的最小产品目标

不要把“追平 DSH 全部功能”作为下一个版本。更可执行的目标是：

> 一个 Python 原生、单 workspace、单用户、Headless 的安全编程 Agent；支持流式 DeepSeek、可恢复 Session、受沙箱保护的 shell/file 工具、审批、取消、上下文压缩和 Skills。

暂缓 Web UI、ACP、多租户、定时任务、多个 Subagent provider 和完整 LSP 矩阵。先完成任务—工具—会话—权限四个闭环，才能让后续插件建立在稳定语义上。

达到这个目标后，可以称为“可用于受控本地开发任务的 alpha”；在此之前，更准确的名称仍是“真实 API Demo”。

## 9. 需要同时修补的 Cordis runtime 瑕疵

Agent 层之外，通用 Python Cordis 还有几项会阻碍 Harness 扩展：

| 优先级 | Runtime 缺口 | 对 Harness 的影响 |
| --- | --- | --- |
| 已补齐 | `ctx.spawn()` / Fiber 与 Effect task scope | Agent 工具、流式请求和后台 Job 可接入；裸 Task 不会自动纳管 |
| P0 | cleanup 无统一 timeout/deadline | 一个坏插件能永久卡住 reload 或进程退出 |
| P1 | Loader reconcile 非全局事务 | 多 Entry patch 中途失败可能形成新旧配置混合态 |
| P1 | 无依赖环诊断 | 能力互相等待时只表现为永久 `PENDING` |
| P1 | HMR 只理解静态 import | 动态 provider、re-export 和 namespace package 可能漏重载 |
| P1 | update hook 无 rollback 契约 | 原地配置更新失败可能留下不可见副作用 |
| 已补齐 | `ServiceKey[T]`、Context 描述符及泛型配置 | 新插件可静态检查服务接口；旧字符串 API 与 Agent 示例的动态调用仍需逐步迁移 |
| P2 | Realm/error/module graph 无资源治理 | 长运行、多 workspace 场景会积累状态和扫描成本 |

这些问题的详细背景见[实现与维护参考的“当前限制与风险”](implementation.md#当前限制与风险)。

## 10. 如何保持这份比较不过期

DSH 仍处于 alpha，内部接口和默认能力变化很快。每次准备追平功能前应：

1. 查看[官方 Releases](https://github.com/deepseek-ai/deepseek-harness/releases)，更新本文版本和日期；
2. 以该 tag 的 architecture、subsystem reference 和源码为准，不以旧博客或二手介绍为准；
3. 用“用户可观察语义 + 验收测试”追踪，不按 DSH 包名逐个搬运；
4. 对已变更或移除的 alpha 能力记录迁移，不假设向后兼容。

`v0.1.2-alpha.5` 本身是预发布版本；其发布说明也显示最近版本仍在调整会话 API、子 Agent 消息、Profile 默认工具和持久化后端。因此本文比较的是一个明确时间点，不是永久规格。[DSH Releases](https://github.com/deepseek-ai/deepseek-harness/releases)

## 11. 官方依据

- [DSH 最新 Releases](https://github.com/deepseek-ai/deepseek-harness/releases)
- [DSH Architecture](https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/architecture.md)
- [DSH Subsystems Index](https://deepseek-harness.github.io/deepseek-harness/en/reference/subsystems/)
- [DSH Core / Agent Lifecycle](https://deepseek-harness.github.io/deepseek-harness/en/reference/subsystems/core)
- [DSH Scoped Registration](https://deepseek-harness.github.io/deepseek-harness/en/reference/subsystems/scope)
- [DSH Sessions](https://deepseek-harness.github.io/deepseek-harness/en/reference/subsystems/session)
- [DSH Persistence](https://deepseek-harness.github.io/deepseek-harness/en/reference/subsystems/persistence)
- [DSH LLM Streaming](https://deepseek-harness.github.io/deepseek-harness/en/reference/subsystems/llm-streaming)
- [DSH Compaction](https://deepseek-harness.github.io/deepseek-harness/en/reference/subsystems/compaction)
- [DSH Tools](https://deepseek-harness.github.io/deepseek-harness/en/reference/subsystems/tools)
- [DSH Sandbox](https://deepseek-harness.github.io/deepseek-harness/en/reference/subsystems/sandbox)
- [DSH Approval](https://deepseek-harness.github.io/deepseek-harness/en/reference/subsystems/approval)
- [DSH Skills](https://deepseek-harness.github.io/deepseek-harness/en/reference/subsystems/skills)
- [DSH Subagent](https://deepseek-harness.github.io/deepseek-harness/en/reference/subsystems/subagent)
