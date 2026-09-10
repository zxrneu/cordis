# Cordis Wiki 知识库

> **A Meta-Framework of Spatiotemporal Composability**  
> 具有时空可组合性的现代化应用元框架。

欢迎来到 Cordis 的技术 Wiki。本 Wiki 全面剖析 Cordis 的**设计初衷、架构蓝图、底层核心机制、扩展系统与实战用法**，帮助开发者从浅入深理解并掌握这一面向复杂、动态、高可扩展性系统的元框架。

---

## 📚 目录导航

| 章节 | 文件 | 核心内容概要 |
| :--- | :--- | :--- |
| **⭐ 开发者完全指南 & QA** | [developer-guide.md](developer-guide.md) | 从零到深入全面指南、契约与生命周期、依赖注入、高级事件、多租户、可调用服务与 10 大高频 QA。 |
| **🛠️ 核心原理走读与维护手册** | [internals-walkthrough.md](internals-walkthrough.md) | 深度源码走读（Proxy 陷阱、Fiber 状态机、Inertia 锁）、内存模型、LIFO 清理流与四大维护铁律。 |
| **01. 为什么需要 Cordis** | [01-why-cordis.md](01-why-cordis.md) | 传统 IoC/插件架构的痛点、时空可组合性（Spatiotemporal Composability）哲学、响应式依赖与全自动副作用管理。 |
| **02. 架构设计与体系结构** | [02-architecture.md](file:///Users/zxr/coding/cordis/doc/02-architecture.md) | Monorepo 模块分层、核心对象关系拓扑（Mermaid 图解）、生命周期与控制流模型。 |
| **03. 核心机制：Context、Fiber 与 Effect** | [03-core-concepts.md](file:///Users/zxr/coding/cordis/doc/03-core-concepts.md) | 原型链上下文派生、Fiber 6 态状态机、惯性锁（Inertia Lock）、生成器与异步 Effect 收集释放机制。 |
| **04. 服务体系与响应式依赖注入** | [04-service-and-injection.md](file:///Users/zxr/coding/cordis/doc/04-service-and-injection.md) | `Service` 基类生命周期、`provide`/`inject` 动态挂载、Stage 3 `@Inject` 装饰器、Callable 服务、Associate 对象追踪。 |
| **05. 高级事件驱动系统** | [05-event-system.md](file:///Users/zxr/coding/cordis/doc/05-event-system.md) | 5 大分发策略（`emit`/`parallel`/`serial`/`bail`/`waterfall`）、上下文过滤与隔离、内部生命周期事件。 |
| **06. 声明式配置与加载器** | [06-loader-and-config.md](file:///Users/zxr/coding/cordis/doc/06-loader-and-config.md) | `EntryTree` 拓扑配置模型、`Include` 动态回写与打补丁、`LocalRealm`/`GlobalRealm` 命名空间隔离、YAML 表达式插值。 |
| **07. 模块热重载（HMR）引擎** | [07-hmr-engine.md](file:///Users/zxr/coding/cordis/doc/07-hmr-engine.md) | Node.js 内部 ESM Loader 劫持、依赖图拓扑推演、Accepted/Declined 算法、双缓存清理与原子回滚。 |
| **08. 官方生态与内置服务** | [08-built-in-plugins.md](file:///Users/zxr/coding/cordis/doc/08-built-in-plugins.md) | `@cordisjs/plugin-timer` 定时器服务、`logger-console` 彩色日志、`plugin-group` 分组、`plugin-utils` 追踪集合。 |
| **09. 从零到一实战开发指南** | [09-user-guide.md](file:///Users/zxr/coding/cordis/doc/09-user-guide.md) | 快速起步、编写自定义插件与服务、配置文件启动、TypeScript 类型提示增强与最佳实践。 |
| **10. TypeScript 高级类型与语法全解** | [10-typescript-guide.md](file:///Users/zxr/coding/cordis/doc/10-typescript-guide.md) | 模块扩充（Module Augmentation）、unique symbol、条件类型与 infer 逆向推导、键重映射、NoInfer、型变标注与装饰器体系。 |

---

## 🌟 核心特性速览

```
               ┌────────────────────────────────────────────────────────┐
               │                  Spatiotemporal Space                  │
               │                                                        │
   Temporal    │   • Reactive Service Lifecycle (Inject / Refresh)     │
  (时间维度)   │   • Fiber State Machine & Inertia Lock                 │
               │   • Automatic Effect / Disposable Cleanup              │
               ├────────────────────────────────────────────────────────┤
   Spatial     │   • Prototypal Context Tree (ctx.extend)               │
  (空间维度)   │   • Namespace Isolation (ctx.isolate / Realms)         │
               │   • Config Interception (ctx.intercept)                │
               └────────────────────────────────────────────────────────┘
```

1. **时空可组合（Spatiotemporal Composability）**：在空间上支持多租户与分层隔离，在时间上支持运行期插件与服务的动态拔插、热重载与级联刷新。
2. **完全管控的副作用体系（Effect Scope）**：无论事件监听器、定时器还是外部资源句柄，均在上下文销毁时自动逆序释放，告别内存泄漏。
3. **响应式依赖注入（Reactive Dependency Injection）**：依赖尚未就绪时插件自动挂起，依赖就绪即刻唤醒；依赖重启时平滑重载。
4. **极度轻量与高性能（Zero Heavyweight Overhead）**：基于 JavaScript 原型链与 Proxy 深度定制，无需庞大的依赖注入容器或运行时反射负担。
5. **声明式拓扑配置与原生 HMR**：支持 YAML/JSON 配置驱动与文件监听，结合 Node.js 内部 Loader 劫持实现零停机的细粒度模块热更。

---

## 📖 核心概念术语表 (Glossary)

- **Context (上下文)**：应用状态与环境的轻量隔离单元，通过原型链逐层继承。
- **Fiber (纤维)**：插件在 Context 中激活并运行的具体实例载体，负责生命周期状态机与副作用收集。
- **Service (服务)**：在 Context 中声明并提供核心功能的对象或类，具有全局或隔离域可见性。
- **Effect (副作用)**：插件运行时产生的外部行为（如添加监听、启动定时器、发起网络连接），以 Disposable 形式被 Fiber 追踪。
- **Inject (注入)**：插件所依赖的服务列表，决定插件何时处于可用激活状态。
- **Isolate (隔离)**：通过特定 Label 将某些服务划分到独立的上下文空间，实现多实例互不干扰。
- **Intercept (拦截)**：针对特定服务在当前 Context 层级中重写或覆盖配置参数。
- **Associate (关联)**：将非 Service 对象（如会话 Session）绑定到上下文追踪系统，自动溯源调用者（Caller）。
- **Inertia Lock (惯性锁)**：防止服务快速频繁切换状态时产生竞态条件的异步重载保护锁。
