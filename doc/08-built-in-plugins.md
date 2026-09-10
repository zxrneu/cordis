# 08. 官方生态与内置服务

Cordis 官方维护了一组经过严格测试与生产验证的核心扩展包，涵盖定时任务、控制台日志、插件容器分组以及上下文感知集合等通用基础设施。

---

## 1. `@cordisjs/plugin-timer`（定时器服务）

在传统的 Node.js 应用中，`setTimeout` 和 `setInterval` 是内存泄漏的重灾区。如果插件在卸载时未手动清理定时器，回调函数将永远驻留后台。

`@cordisjs/plugin-timer` 提供了生命周期全托管的定时器能力，并将方法直接混入到 `Context` 对象中：

### 1.1 核心 API 详解

```typescript
import TimerService from '@cordisjs/plugin-timer'
ctx.plugin(TimerService)
```

#### 1. `ctx.timeout`
支持传统回调模式与现代 `Promise`（sleep）模式。**当上下文被销毁时，正在等待的 Promise 会被自动 reject 并清理底层定时器**：

```typescript
// 回调模式：返回清理句柄
const dispose = ctx.timeout(() => {
  console.log('Fired!')
}, 1000)

// Promise / Sleep 模式：
await ctx.timeout(1000)
console.log('1 second passed!')
```

#### 2. `ctx.interval`
支持传统循环回调模式与 **`AsyncIterable` 异步流式迭代器模式**：

```typescript
// 传统模式
const dispose = ctx.interval(() => {
  pollStatus()
}, 5000)

// 现代异步流式迭代：
for await (const _ of ctx.interval(1000)) {
  console.log('Heartbeat tick')
  // 当插件被卸载时，迭代器自动终止，安全退出循环！
}
```

#### 3. `ctx.throttle`（节流）与 `ctx.debounce`（防抖）
创建的包装函数不仅能在规定时间内限制调用频率，还自带 `.dispose()` 方法，并在上下文卸载时自动取消挂起的延迟调用：

```typescript
const handleResize = ctx.debounce((width: number) => {
  console.log('Resized to', width)
}, 200)

window.onresize = () => handleResize(window.innerWidth)
```

---

## 2. `@cordisjs/plugin-logger-console`（终端炫彩日志）

`@cordisjs/plugin-logger-console` 是 Cordis 的默认控制台日志导出器（`Exporter`），专为生产环境调试与开发者体验打造。

### 2.1 核心特性
- **服务名自动哈希着色**：不同服务名输出的日志标签自动分配不同颜色，视觉上层次分明。
- **调用耗时差（Time Diff）**：开启 `showDiff: true` 后，同名服务的连续日志后会追加 `+12ms` 耗时提示，性能瓶颈一目了然。
- **多行文本自动缩进**：输出包含换行符的对象或堆栈时，后续行自动与首行内容对齐，保持终端排版整洁。
- **灵活的占位符支持**：支持 `%s`（字符串）、`%d`/`%i`（整数）、`%o`/`%O`（对象结构）、`%C`（高亮颜色代码）。

```typescript
import ConsoleExporter from '@cordisjs/plugin-logger-console'

ctx.plugin(ConsoleExporter, {
  colors: 2, // 启用 256 色支持
  showTime: 'yyyy-MM-dd hh:mm:ss ',
  showDiff: true,
  levels: {
    'database': 3, // 针对特定服务开启 DEBUG 级别
    'default': 2,  // 其余服务保持 INFO 级别
  },
})

// 使用日志
const logger = ctx.logger('database')
logger.info('Connected to PostgreSQL at %s:%d', 'localhost', 5432)
```

---

## 3. `@cordisjs/plugin-group`（插件容器分组）

在构建大型系统时，常常需要将多个相关联的插件组织在一起进行统一管理：

```yaml
# 在 cordis.yml 中使用 group
- name: '@cordisjs/plugin-group'
  config:
    - name: './plugins/auth'
    - name: './plugins/rbac'
    - name: './plugins/audit'
```

- **批量控制**：禁用或启用 Group 时，内部所有子插件自动级联生效。
- **嵌套结构**：Group 支持无限层级嵌套，构建清晰的模块层级体系。

---

## 4. `@cordisjs/plugin-utils`（上下文感知集合）

在很多场景下，服务需要维护一个全局列表（如路由注册表、中间件队列、协议适配器列表）。当某个插件向该列表中添加元素时，一旦该插件卸载，列表中对应的元素必须被同步移除。

`@cordisjs/plugin-utils` 提供了 `List<T>`：

```typescript
import { List } from '@cordisjs/plugin-utils'

class RouterService extends Service {
  public routes = new List<Route>(this.ctx, 'router.routes')

  constructor(ctx: Context) {
    super(ctx, 'router')
  }
}

// 某个业务插件：
function AuthPlugin(ctx: Context) {
  // 向路由列表推入新路由
  ctx.router.routes.push({ path: '/login', handler: loginHandler })
  // 当 AuthPlugin 卸载时，/login 路由自动从 router.routes 中被剔除！
}
```
