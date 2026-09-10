# 09. 从零到一实战开发指南

本章将通过一个完整的实战演练，带领你从零搭建一个基于 Cordis 的模块化、自愈型微型应用，体验 Cordis 的类型系统、响应式注入与生命周期管控。

---

## 1. 项目初始化

### 方式 A：使用官方脚手架一键生成

```bash
npm create cordis@latest my-app
# 或者
yarn create cordis my-app
```

### 方式 B：手动配置项目

创建 `package.json`：
```json
{
  "name": "my-cordis-app",
  "type": "module",
  "scripts": {
    "start": "node --expose-internals bin.js"
  },
  "dependencies": {
    "cordis": "^4.0.0-rc.8",
    "@cordisjs/plugin-loader": "^1.0.0-rc.5",
    "@cordisjs/plugin-include": "^1.0.4",
    "@cordisjs/plugin-timer": "^1.0.0",
    "@cordisjs/plugin-logger-console": "^1.0.0"
  }
}
```

---

## 2. 编写第 1 个服务：配置与类型声明（TypeScript 友好）

在 TypeScript 中使用 Cordis 时，推荐通过模块扩充（Module Augmentation）让 `ctx.<service>` 获得强类型提示：

```typescript
// src/services/database.ts
import { Context, Service } from 'cordis'

// 1. 扩充 Context 类型定义
declare module 'cordis' {
  interface Context {
    database: DatabaseService
  }
}

export interface DatabaseConfig {
  connectionString: string
}

// 2. 实现 Service 服务类
export class DatabaseService extends Service {
  private connected = false

  constructor(ctx: Context, public config: DatabaseConfig) {
    super(ctx, 'database')
  }

  // 异步初始化
  async [Service.init]() {
    this.ctx.logger.info('Connecting to database: %s', this.config.connectionString)
    // 模拟建立连接
    await this.ctx.timeout(500)
    this.connected = true
    this.ctx.logger.info('Database connected successfully!')

    // 返回清理函数
    return () => {
      this.connected = false
      this.ctx.logger.info('Database connection closed.')
    }
  }

  async queryUser(id: string) {
    if (!this.connected) throw new Error('Database is offline!')
    return { id, name: 'Alice', role: 'admin' }
  }
}
```

---

## 3. 编写第 2 个插件：消费服务与事件分发

现在编写一个依赖于 `database` 的业务插件：

```typescript
// src/plugins/user-manager.ts
import { Context } from 'cordis'

// 扩充自定义事件类型
declare module 'cordis' {
  interface Events {
    'user/registered'(user: { id: string; name: string }): void
  }
}

export const UserManagerPlugin = {
  name: 'user-manager',
  // 显式声明依赖 database 和 timer 服务
  inject: ['database', 'timer'],

  apply(ctx: Context) {
    ctx.logger.info('UserManager initialized, database is ready!')

    // 监听内部事件
    ctx.on('user/registered', (user) => {
      ctx.logger.info('New user event received for: %s', user.name)
    })

    // 定时轮询任务（受上下文自动清理保护）
    ctx.interval(async () => {
      const user = await ctx.database.queryUser('1001')
      ctx.logger.debug('Polling user status: %o', user)
    }, 5000)

    // 触发事件
    ctx.emit('user/registered', { id: '1001', name: 'Alice' })
  },
}
```

---

## 4. 组装并启动应用（程序化启动）

```typescript
// src/index.ts
import { Context } from 'cordis'
import ConsoleExporter from '@cordisjs/plugin-logger-console'
import TimerService from '@cordisjs/plugin-timer'
import { DatabaseService } from './services/database'
import { UserManagerPlugin } from './plugins/user-manager'

const ctx = new Context()

// 1. 挂载基础设施
ctx.plugin(ConsoleExporter, { showDiff: true })
ctx.plugin(TimerService)

// 2. 挂载业务插件（即使先挂载依赖方，也能自动等待服务提供方就绪）
ctx.plugin(UserManagerPlugin)

// 3. 挂载服务提供方
ctx.plugin(DatabaseService, {
  connectionString: 'postgres://localhost:5432/myapp',
})
```

运行输出效果：
```
[I] logger-console  watching root in /Users/...
[I] database        Connecting to database: postgres://localhost:5432/myapp
[I] database        Database connected successfully! +502ms
[I] user-manager    UserManager initialized, database is ready!
[I] user-manager    New user event received for: Alice
```

---

## 5. 声明式配置模式（基于 `cordis.yml`）

如果你希望将拓扑编排交给声明式配置驱动，创建 `cordis.yml`：

```yaml
# cordis.yml
- name: '@cordisjs/plugin-logger-console'
  config:
    showDiff: true

- name: '@cordisjs/plugin-timer'

- name: './src/services/database'
  config:
    connectionString: !js process.env.DB_URL || 'postgres://localhost:5432/prod'

- name: './src/plugins/user-manager'
```

入口文件仅需极简的三行：

```javascript
// bin.js
import { Context } from 'cordis'
import Loader from '@cordisjs/plugin-loader'
import Include from '@cordisjs/plugin-include'

const ctx = new Context()
await ctx.plugin(Loader)
await ctx.plugin(Include, { path: './cordis.yml' })
```

执行 `node bin.js` 即可自动解析整个应用拓扑！

---

## 6. 生产最佳实践清单

1. **永远使用 `ctx.on` / `ctx.effect` / `ctx.timeout` 管理副作用**：
   - 避免直接在插件中使用全局 `setTimeout` 或全局事件监听，确保模块在热更或卸载时零资源残留。
2. **严守 `inject` 依赖边界**：
   - 访问任何服务前，务必在插件或类的 `inject` 列表中声明，利用 Cordis 的安全检查机制规避未定义调用。
3. **合理利用 `ctx.isolate()` 划定多租户边界**：
   - 对于需要运行多份异构实例（如多数据库源、多代理配置）的场景，采用 `isolate` 隔离，避免互相覆盖冲突。
4. **推荐 Standard Schema 规范配置校验**：
   - 使用 Zod 或 Schemastery 为插件的 `Config` 编写 Schema，享受自动类型推导与启动期安全校验。
