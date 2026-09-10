# 04. 服务体系与响应式依赖注入

在 Cordis 中，**服务（Service）** 是实现业务能力共享、功能抽象与跨模块协作的核心机制。本章将详细介绍服务的定义、生命周期管理、依赖声明方式（包括 Stage 3 装饰器）以及响应式依赖联动原理。

---

## 1. Service 基类体系

所有服务推荐继承自 `cordis` 导出的抽象基类 `Service`。

```typescript
import { Context, Service } from 'cordis'

export class DatabaseService extends Service {
  // 声明该服务所依赖的其他服务
  static inject = ['logger']

  constructor(ctx: Context, config: DatabaseConfig) {
    // 调用 super 自动完成服务注册
    super(ctx, 'database')
  }

  async query(sql: string) {
    this.ctx.logger.info('Executing SQL: %s', sql)
    // ...
  }
}
```

### 1.1 核心 Symbol 钩子

`Service` 抽象类定义了一系列 Symbol 钩子，赋能强大的生命周期控制：

| 钩子 / 属性 | 作用与机制 |
| :--- | :--- |
| **`Service.init`** (`symbols.init`) | **异步初始化钩子**。当服务实例被创建时自动触发。可以返回一个清理函数或生成器，并在所有异步动作完成前阻塞下游依赖该服务的插件激活。 |
| **`Service.check`** (`symbols.check`) | **可用性断言钩子**。定义服务在何种条件下被判定为“就绪”。返回 `false` 时，依赖该服务的下游插件将继续保持挂起。 |
| **`Service.invoke`** (`symbols.invoke`) | **函数式服务钩子**。当服务自身需要能够像函数一样被直接调用时使用（Callable Service）。 |
| **`Service.extend`** (`symbols.extend`) | **派生克隆钩子**。用于扩展生成新服务代理。 |
| **`Service.tracker`** (`symbols.tracker`) | **关联追踪器钩子**。用于将非 Service 类（如 Session）绑定到 Cordis 上下文系统。 |

---

## 2. 声明与注入依赖（Inject）

Cordis 支持多种灵活的依赖声明方式：

### 2.1 静态属性声明（Class / Object Plugin）

```typescript
// 方式 A：数组形式
class MyPlugin {
  static inject = ['database', 'timer']
  constructor(ctx: Context) {
    // 此时 ctx.database 和 ctx.timer 一定处于 ACTIVE 状态
  }
}

// 方式 B：对象形式（可携带拦截配置）
const ConsumerPlugin = {
  inject: {
    database: null,
    logger: { level: 3 }, // 覆盖 logger 配置
  },
  apply(ctx: Context) {
    // ...
  },
}
```

### 2.2 运行时动态注入（Dynamic Inject）

在需要临时按需使用某种服务的场景下，无需重构整个插件结构：

```typescript
ctx.inject(['database'], (ctx) => {
  // 当 database 存在且就绪时，该回调才会执行
  ctx.database.query('SELECT 1')

  return () => {
    // 当 database 卸载或插件注销时执行清理
  }
})
```

### 2.3 Stage 3 标准装饰器（`@Inject`）

Cordis 提供了符合 ECMAScript Stage 3 规范的 `@Inject` 装饰器，支持装饰**类**以及**类方法**：

```typescript
import { Context, Inject, Service } from 'cordis'

class WorkerService extends Service {
  constructor(ctx: Context) {
    super(ctx, 'worker')
  }

  // 1. 类级别装饰器：声明该类被挂载时所需的依赖
  // @Inject('database')

  // 2. 方法级别装饰器：该方法仅在特定依赖就绪时动态绑定执行！
  @Inject('cache')
  syncCache() {
    this.ctx.logger.info('Cache is available, starting sync...')
    return () => {
      this.ctx.logger.info('Cache was disposed, stopping sync...')
    }
  }
}
```

- 当被装饰的方法所依赖的服务上线时，该方法会自动执行并将其返回的清理函数注册到当前 Fiber 中；当服务下线时，清理函数自动执行。

---

## 3. 响应式依赖联动机制（Reactive Dependency Mechanism）

传统框架在服务上下线时往往束手无策，而 Cordis 具备天然的响应式联动能力：

```mermaid
flowchart TD
    A[服务状态变更: ctx.provide / dispose] --> B[ReflectService.notify(names)]
    B --> C[扫描 Registry 中所有 Runtime 与 Fibers]
    C --> D{Fiber 是否声明依赖该服务?}
    D -- 否 --> E[跳过忽略]
    D -- 是 --> F[执行 fiber._checkImpl(name)]
    F --> G{依赖是否全部满足?}
    G -- 满足 --> H[计算新 Epoch -> 触发 _reload()]
    G -- 缺失 --> I[计算新 Epoch -> 触发 _unload() -> 挂起为 PENDING]
    H --> J[向下广播 internal/service 事件]
    I --> J
```

### 联动特性的核心优势
1. **启动顺序无关（Order Independence）**：无论 `Consumer` 先加载还是 `Provider` 先加载，最终系统都会在 `Provider` 就绪后自动将 `Consumer` 带入 `ACTIVE` 状态。
2. **热拔插零残留**：当 `Provider` 插件被卸载或代码热更新时，所有依赖它的 `Consumer` 会自动执行局部卸载，等待新的 `Provider` 实例化完成后再行恢复。

---

## 4. 函数式服务（Callable Service）

有些服务除了提供属性和方法外，自身也是一个可调用函数（如 `ctx.logger('my-tag')`）。在 Cordis 中，只需在服务类中实现 `[Service.invoke]`：

```typescript
export class FormatterService extends Service {
  constructor(ctx: Context) {
    super(ctx, 'formatter')
  }

  // 实现 Callable 逻辑
  protected [Service.invoke](input: string): string {
    return input.trim().toUpperCase()
  }

  public helperMethod() {
    // 其他普通方法
  }
}

// 使用方式：既可以作为函数调用，也可以作为对象使用
ctx.formatter(' hello ') // 返回 'HELLO'
ctx.formatter.helperMethod()
```

---

## 5. 关联对象追踪（Associate & Caller Tracking）

在复杂应用中，往往存在许多不是 `Service` 的临时对象（例如网络连接会话 `Session`、消息事件上下文）。我们希望在这些对象上调用方法时，能够**溯源到究竟是哪个上下文或插件发起的调用**。

Cordis 通过 `[Service.tracker]` 实现了高精度的 Caller 追踪：

```typescript
export class Session {
  [Service.tracker] = {
    property: 'ctx',
    associate: 'session',
  }

  constructor(public ctx: Context) {}
}

export class SessionService extends Service {
  constructor(ctx: Context) {
    super(ctx, 'session')

    // 将 session.send 混入到关联对象
    ctx.mixin(this, {
      send: 'session.send',
    })
  }

  send(message: string) {
    // (this as any)[symbols.caller] 能精准定位是哪个插件上下文调用了 session.send()
    const caller = (this as any)[symbols.caller] as Context
    caller.logger.info('Send message from: %s', caller.fiber.name)
  }
}
```

---

## 6. 方法混入（Mixin）与属性访问器（Accessor）

- **`ctx.mixin(source, mixins)`**：将某个服务的方法直接投影并混入到 Context 对象自身，简化常用 API 的调用路径（例如将 `ctx.timer.timeout` 混入为 `ctx.timeout`）。
- **`ctx.accessor(name, options)`**：在 Context 上定义动态属性的 getter/setter，并在上下文销毁时自动移除。
