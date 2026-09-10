# 05. 高级事件驱动系统

事件驱动（Event-Driven）是解耦复杂业务模块最常用的设计模式之一。Node.js 原生提供了 `EventEmitter`，但其仅支持最基础的同步广播，且缺乏生命周期关联与上下文感知。

Cordis 重新设计了事件服务（`EventsService`），提供**生命周期自动绑定、5 大分发策略、上下文过滤与责任链流水线**。

---

## 1. 为什么超越 Node.js 原生 EventEmitter？

| 痛点 | Node.js 原生 `EventEmitter` | Cordis `EventsService` |
| :--- | :--- | :--- |
| **生命周期绑定** | 必须手动 `removeListener`，极易遗漏引发内存泄漏 | 自动绑定到当前 `Fiber`，插件卸载时自动清理 |
| **异步并发处理** | 不支持等待异步 Listener 完成 | 原生支持 `parallel`（并发聚合）与 `serial`（异步串行） |
| **熔断与拦截机制** | 无法捕获 Listener 返回值进行中断 | 支持 `bail`（同步短路）与 `serial` 熔断拦截 |
| **洋葱圈流水线** | 不支持中间件责任链 | 支持 `waterfall` 瀑布流拦截 |
| **上下文过滤** | 全局无差别广播 | 支持通过 `thisArg` 与 `Context.filter` 进行作用域过滤 |

---

## 2. 五大事件分发策略

在 Cordis 中，同一事件名可以根据实际业务诉求以不同的策略进行分发：

```
                    Cordis 5 大分发策略
   ┌─────────────────────────────────────────────────────────────┐
   │ 1. emit      ──> 同步广播，按序调用所有监听器               │
   │ 2. parallel  ──> 异步并行，Promise.all 并发等待并聚合错误   │
   │ 3. serial    ──> 异步串行，依次 await 执行，遇非空返回值熔断│
   │ 4. bail      ──> 同步熔断，遇首个非空返回值立即返回中断     │
   │ 5. waterfall ──> 洋葱圈责任链，嵌套传递 next 闭包处理参数   │
   └─────────────────────────────────────────────────────────────┘
```

### 2.1 `ctx.emit(name, ...args)`（同步广播）
最基础的分发方式，同步按序执行所有监听器，不捕获返回值。

```typescript
ctx.on('user/login', (user) => {
  console.log('User logged in:', user.name)
})

ctx.emit('user/login', { name: 'Alice' })
```

### 2.2 `ctx.parallel(name, ...args)`（异步并行）
并发执行所有监听器，并等待所有异步操作全部完成。如果某些监听器抛出异常，Cordis 不会中途短路，而是使用 `Promise.allSettled` 收集所有错误，并在最后抛出合并后的 `AggregateError`：

```typescript
ctx.on('system/ready', async () => {
  await loadCache()
})
ctx.on('system/ready', async () => {
  await syncDatabase()
})

// 并行等待所有任务完成
await ctx.parallel('system/ready')
```

### 2.3 `ctx.serial(name, ...args)`（异步串行与熔断）
按顺序依次 `await` 执行监听器。若某个监听器返回了一个有效值（满足 `isBailed`: 非 `null`、非 `undefined`、非 `false`），执行链立即熔断，并将该结果返回：

```typescript
ctx.on('auth/check', async (token) => {
  if (isBlacklisted(token)) return { allowed: false, reason: 'blacklisted' }
})
ctx.on('auth/check', async (token) => {
  const user = await verifyToken(token)
  if (user) return { allowed: true, user }
})

// 遇首个有确定结论的监听器即刻停止后续检查
const result = await ctx.serial('auth/check', userToken)
```

### 2.4 `ctx.bail(name, ...args)`（同步熔断）
同步版本的短路策略。常用于同步权限检查、规则匹配或默认值计算：

```typescript
ctx.on('calculate/price', (item) => {
  if (item.isVip) return item.price * 0.8
})

const finalPrice = ctx.bail('calculate/price', currentItem) ?? currentItem.price
```

### 2.5 `ctx.waterfall(name, ...args, next)`（洋葱圈责任链）
类似 Koa 中间件机制，允许监听器对输入参数进行拦截、修改，并决定是否调用 `next()` 传递给下一个处理器：

```typescript
ctx.on('http/request', (req, next) => {
  req.headers['x-custom'] = 'injected'
  return next() // 继续执行下游
})

ctx.on('http/request', (req, next) => {
  if (!req.auth) return { status: 401 } // 拦截不向下传递
  return next()
})

const response = ctx.waterfall('http/request', requestObject, (req) => {
  // 最终的实际处理业务
  return { status: 200, data: 'ok' }
})
```

---

## 3. 上下文过滤与作用域隔离（Context Filtering）

在多租户、多会话或隔离环境中，往往需要限制事件只在特定范围内传播。Cordis 允许在分发事件时传入一个拥有 `Context.filter` 的 `thisArg` 对象：

```typescript
class Session {
  constructor(public id: string) {}

  // 定义当前 session 仅接受属于同 ID 上下文的监听器
  [Context.filter](targetCtx: Context) {
    return targetCtx.session?.id === this.id
  }
}

const sessionA = new Session('A')
const sessionB = new Session('B')

// 只有在 sessionA 作用域内注册的监听器才会被触发
ctx.emit(sessionA, 'message', 'Hello Group A!')
```

---

## 4. 内部核心生命周期事件清单

Cordis 内部通过事件总线管理其自身的全部状态机变化，开发者或上层插件（如 Loader、HMR）可以订阅这些内部事件以实现深度扩展：

| 事件名称 | 参数签名 | 触发时机与用途 |
| :--- | :--- | :--- |
| `internal/plugin` | `(fiber: Fiber)` | 当新插件被实例化创建，或插件被销毁时触发。 |
| `internal/status` | `(fiber: Fiber, oldState: FiberState)` | 当 Fiber 状态机发生跃迁（如 `PENDING` -> `ACTIVE`）时触发。 |
| `internal/service` | `(name: string, value: any)` | 当某个 Service 被提供、更新或注销时触发。 |
| `internal/update` | `(config: any, noSave: boolean, next: () => void)` | 当配置更新发生时触发（支持打补丁与回写配置）。 |
| `internal/get` | `(ctx: Context, name: string, error: Error, next: () => any)` | 拦截 Context 属性读取过程。 |
| `internal/set` | `(ctx: Context, name: string, value: any, error: Error, next: () => boolean)` | 拦截 Context 属性赋值过程。 |
| `internal/listener`| `(name: string, listener: any, options: EventOptions)` | 监听器注册时的前置拦截。 |
| `internal/dispatch`| `(mode: DispatchMode, name: string, args: any[], thisArg: any)` | 任何全局事件被分发时的审计与拦截钩子。 |
