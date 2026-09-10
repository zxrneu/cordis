/**
 * 示例 2: 自定义 Service、类型扩充与异步生命周期
 * 
 * 运行方式:
 *   npx tsx examples/02-custom-service.ts
 */

import { Context, Service } from 'cordis'

// 1. 类型扩充 (Module Augmentation)
// 让 TypeScript 编译器知道 ctx 上挂载了 database 服务
declare module 'cordis' {
  interface Context {
    database: DatabaseService
  }
}

export interface DatabaseConfig {
  connectionString: string
}

// 2. 继承 Service 基类
export class DatabaseService extends Service {
  public connected = false
  private records = new Map<string, any>()

  constructor(ctx: Context, public config: DatabaseConfig) {
    // 传入 ctx 与服务唯一标识名 'database'
    // Cordis 自动将其实例挂载到 ctx.database
    super(ctx, 'database')
  }

  /**
   * [Service.init] 异步初始化钩子
   * 当服务实例化且依赖就绪时自动调用。
   * 支持返回 Dispose 清理函数，在服务卸载时执行。
   */
  async [Service.init]() {
    console.log(`[DatabaseService] 正在连接数据库: ${this.config.connectionString}...`)
    
    // 模拟网络延迟建立连接
    await new Promise((resolve) => setTimeout(resolve, 150))
    this.connected = true
    console.log(`[DatabaseService] 数据库连接就绪！`)

    // 返回销毁清理函数
    return () => {
      this.connected = false
      this.records.clear()
      console.log(`[DatabaseService] 数据库连接已安全断开，数据缓存已清空。`)
    }
  }

  // 业务方法
  async insert(id: string, data: any) {
    if (!this.connected) {
      throw new Error('Database is offline!')
    }
    this.records.set(id, data)
    console.log(`[DatabaseService] 写入数据成功 -> key: ${id}`)
  }

  get(id: string) {
    return this.records.get(id)
  }
}

async function main() {
  const app = new Context()

  console.log('--- 挂载 DatabaseService ---')
  const dbFiber = app.plugin(DatabaseService, {
    connectionString: 'postgres://admin:secret@127.0.0.1:5432/app_db',
  })

  // 等待服务完成异步 [Service.init]
  await dbFiber

  console.log('\n--- 业务调用 ---')
  await app.database.insert('user:101', { name: 'Alice', role: 'admin' })
  console.log('读取数据:', app.database.get('user:101'))

  console.log('\n--- 卸载服务 ---')
  await dbFiber.dispose()
  console.log('服务已卸载，当前连接状态:', app.database)
}

main().catch(console.error)
