/**
 * ==============================================================================
 * Cordis 核心机制与实战演练完整 Demo
 * ==============================================================================
 * 
 * 本 Demo 涵盖 Cordis 最核心的五大设计哲学与实战用法：
 * 1. 【Context & TypeScript 扩展】类型安全的上下文与模块扩充
 * 2. 【Service 抽象】自定义服务提供者、异步生命周期 [Service.init] 与清理
 * 3. 【Plugin 插件系统】依赖声明 (inject)、副作用追踪与自动隔离
 * 4. 【自愈与热插拔】依赖未就绪时自动挂起、服务上下线自动自愈、0 内存泄漏
 * 5. 【高级事件派发】emit / parallel / serial / bail / waterfall 五种调度流
 * ==============================================================================
 */

import { Context, Service } from 'cordis'
// ==============================================================================
// 1. 类型扩充 (Module Augmentation)
// 让 ctx.database 和自定义事件拥有完美的 TypeScript 代码补全与类型检查
// ==============================================================================
declare module 'cordis' {
  // 扩充 Context 接口，声明全局可用的服务
  interface Context {
    database: DatabaseService
  }

  // 扩充 Events 接口，声明全局事件名及参数类型
  interface Events {
    'user/create'(user: { id: string; name: string }): void
    'user/auth'(token: string): boolean | string // 用于 bail 熔断拦截
    'user/transform'(data: string, next: () => string): string // 用于 waterfall 洋葱模型
  }
}

// ==============================================================================
// 2. 实现一个 Service（服务提供方）
// ==============================================================================
export interface DatabaseConfig {
  connectionString: string
}

export class DatabaseService extends Service {
  // 标记服务名称，Cordis 会将该服务挂载到 ctx.database
  public isConnected = false
  private dataStore = new Map<string, { id: string; name: string }>()

  constructor(ctx: Context, public config: DatabaseConfig) {
    // 传入 ctx 与 service 唯一标识名 'database'
    super(ctx, 'database')
  }

  /**
   * [Service.init] 异步初始化钩子
   * 当服务实例化且所有内部依赖满足时调用。
   * 支持返回一个清理函数（Dispose），在服务被卸载时自动调用。
   */
  async [Service.init]() {
    console.log(`\n  🔌 [DatabaseService] 正在连接数据库: ${this.config.connectionString}...`)
    
    // 模拟 100ms 网络异步连接
    await new Promise((resolve) => setTimeout(resolve, 100))
    this.isConnected = true
    console.log(`  ✅ [DatabaseService] 数据库连接成功！`)

    // 返回销毁清理函数（重要：保证资源不泄漏）
    return () => {
      this.isConnected = false
      this.dataStore.clear()
      console.log(`  🛑 [DatabaseService] 数据库连接已断开，资源已释放。`)
    }
  }

  async saveUser(id: string, name: string) {
    if (!this.isConnected) {
      throw new Error('Database is offline!')
    }
    this.dataStore.set(id, { id, name })
    console.log(`  💾 [DatabaseService] 保存用户成功 -> ID: ${id}, 姓名: ${name}`)
    // 触发全局事件
    this.ctx.emit('user/create', { id, name })
  }

  getUser(id: string) {
    return this.dataStore.get(id)
  }
}

// ==============================================================================
// 3. 实现业务 Plugin（插件消费方）
// ==============================================================================
export const UserLoggerPlugin = {
  name: 'user-logger-plugin',

  // 显式声明依赖 database 服务：
  // 只有当 database 服务已挂载且 active 时，本插件才会真正激活执行 apply！
  inject: ['database'],

  apply(ctx: Context) {
    console.log(`  🚀 [UserLoggerPlugin] 依赖满足，插件成功激活！`)

    // 1. 监听事件 (监听器会被当前 Context/Fiber 自动追踪，卸载时自动销毁，零内存泄漏)
    ctx.on('user/create', (user) => {
      console.log(`  📢 [UserLoggerPlugin 捕获事件] 收到新用户创建通知: ${user.name} (ID: ${user.id})`)
    })

    // 2. 副作用管控 (ctx.effect 可以注册定时器、Socket、观察者等任意可回收资源)
    ctx.effect(() => {
      console.log(`  ⚙️ [UserLoggerPlugin] 启动心跳监听定时器...`)
      const timer = setInterval(() => {
        // 访问依赖的服务
        const status = ctx.database.isConnected ? 'Healthy' : 'Disconnected'
        console.log(`  💓 [UserLoggerPlugin 心跳] 数据库状态: ${status}`)
      }, 500)

      // 返回清理函数：当插件卸载或依赖服务下线时，Cordis 自动执行此清理
      return () => {
        clearInterval(timer)
        console.log(`  🧹 [UserLoggerPlugin] 自动清理心跳定时器（零内存泄漏！）`)
      }
    })
  },
}

// ==============================================================================
// 4. 主流程演示与自愈特性验证
// ==============================================================================
async function main() {
  console.log('================================================================')
  console.log(' Cordis 响应式生命周期与插件机制演练')
  console.log('================================================================\n')

  // 1. 创建根上下文
  const app = new Context()

  // ----------------------------------------------------------------------------
  // 场景 A：无序加载与等待机制 (Out-of-Order Loading)
  // 先加载依赖 database 的插件，此时 database 服务尚不存在！
  // ----------------------------------------------------------------------------
  console.log('【步骤 1】挂载 UserLoggerPlugin（此时尚未挂载 DatabaseService）...')
  const pluginFiber = app.plugin(UserLoggerPlugin)
  console.log(`👉 插件状态: 挂起等待中 (Pending)，因为缺少 inject: ['database'] 依赖。`)

  await new Promise((r) => setTimeout(r, 200))

  // ----------------------------------------------------------------------------
  // 场景 B：服务上线，依赖自动就绪并触发插件激活 (Reactive Activation)
  // ----------------------------------------------------------------------------
  console.log('\n【步骤 2】挂载 DatabaseService 服务提供方...')
  const dbFiber = app.plugin(DatabaseService, {
    connectionString: 'postgres://admin:secret@localhost:5432/cordis_demo',
  })

  // 等待 DatabaseService 初始化完成
  await dbFiber

  // 业务调用
  console.log('\n【步骤 3】调用服务方法，触发事件流...')
  await app.database.saveUser('u_1001', 'Alice')
  await app.database.saveUser('u_1002', 'Bob')

  // 观察心跳运行
  await new Promise((r) => setTimeout(r, 1100))

  // ----------------------------------------------------------------------------
  // 场景 C：自愈与依赖热卸载 (Dynamic Hot-Unplugging & Zero Leaks)
  // 当 database 服务被卸载时，依赖它的 UserLoggerPlugin 必须自动暂停并清理副作用！
  // ----------------------------------------------------------------------------
  console.log('\n【步骤 4】卸载 DatabaseService (模拟数据库连接断开/服务热重载)...')
  await dbFiber.dispose()
  console.log(`👉 DatabaseService 已销毁，UserLoggerPlugin 的定时器和事件监听已被 Cordis 自动清除！`)

  // 等待一小会儿，验证心跳是否停止（不会再打印心跳日志）
  await new Promise((r) => setTimeout(r, 800))

  // ----------------------------------------------------------------------------
  // 场景 D：服务重新上线，插件自动自愈并重新激活 (Self-Healing)
  // ----------------------------------------------------------------------------
  console.log('\n【步骤 5】重新挂载 DatabaseService (服务自愈演示)...')
  const newDbFiber = app.plugin(DatabaseService, {
    connectionString: 'postgres://admin:secret@localhost:5432/cordis_demo_v2',
  })
  await newDbFiber

  await app.database.saveUser('u_1003', 'Charlie')
  await new Promise((r) => setTimeout(r, 600))

  // ----------------------------------------------------------------------------
  // 场景 E：Cordis 高级事件流（Parallel / Bail / Waterfall）
  // ----------------------------------------------------------------------------
  console.log('\n================================================================')
  console.log('【步骤 6】高级事件分发机制演练 (Bail 熔断与 Waterfall 管道)')
  console.log('================================================================')

  // 1. Bail 机制演示（用于权限校验、拦截器：任意监听器返回有效值即短路退出）
  app.on('user/auth', (token) => {
    if (token === 'valid_admin') return true
    if (token === 'banned_user') return 'User is banned'
    return undefined // 返回 undefined 表示不拦截，继续走下一个
  })

  console.log(`\n🛡️ [Bail 权限校验测试]:`)
  const auth1 = app.bail('user/auth', 'banned_user')
  console.log(`  Token: banned_user -> 拦截结果: "${auth1}"`)

  const auth2 = app.bail('user/auth', 'valid_admin')
  console.log(`  Token: valid_admin -> 校验结果: ${auth2}`)

  const auth3 = app.bail('user/auth', 'unknown_token')
  console.log(`  Token: unknown_token -> 校验结果: ${auth3} (未命中，返回 undefined)`)

  // 2. Waterfall 机制演示（洋葱模型 / 中间件链：类似 Koa/Express，通过 next() 穿透和包装返回值）
  app.on('user/transform', (text, next) => {
    console.log('3')
    return next().trim()
  })
  app.on('user/transform', (text, next) => {
    console.log('2')
    return `[Cordis-Header] -> ${next()}`
  })
  app.on('user/transform', (text, next) => {
    console.log('1')
    return next().toUpperCase()
  })

  console.log(`\n🌊 [Waterfall 洋葱模型中间件测试]:`)
  const initial = '   hello cordis microkernel architecture   '
  // waterfall 最后一个参数是底层默认处理函数 (inner handler)
  const transformed = app.waterfall('user/transform', initial, () => initial)
  console.log(`  原始数据: "${initial}"`)
  console.log(`  处理结果: "${transformed}"`)

  console.log('\n🎉 Cordis Demo 运行全部完成！')
  process.exit(0)
}

main().catch(console.error)
