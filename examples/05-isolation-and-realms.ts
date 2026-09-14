/**
 * 示例 5: 服务隔离域 (Realm) 与多租户多实例架构
 * 
 * 运行方式:
 *   npx tsx examples/05-isolation-and-realms.ts
 */

import { Context, Service } from 'cordis'

declare module 'cordis' {
  interface Context {
    storage: StorageService
  }
}

class StorageService extends Service {
  private memory = new Map<string, string>()

  constructor(ctx: Context, public label: string) {
    super(ctx, 'storage')
  }

  write(key: string, value: string) {
    this.memory.set(key, value)
  }

  read(key: string) {
    return this.memory.get(key)
  }

  dump() {
    return Object.fromEntries(this.memory)
  }
}

// 相同的通用业务插件
const appBusinessPlugin = {
  name: 'app-business',
  inject: ['storage'],
  apply(ctx: Context, tenantId: string) {
    ctx.storage.write('tenant_id', tenantId)
    ctx.storage.write('last_active', new Date().toISOString())
    console.log(`  [Plugin for ${tenantId}] 写入存储 ->`, ctx.storage.dump())
  },
}

async function main() {
  const root = new Context()

  console.log('================================================================')
  console.log('1. ctx.isolate: 隔离命名空间实现多租户多实例')
  console.log('================================================================')

  // 派生两个相互隔离的上下文分枝
  // 每个分枝下的 'storage' 服务均拥有独立的 Realm 标记
  const tenantA: Context = root.isolate('storage')
  const tenantB: Context = root.isolate('storage')

  console.log('-> 为租户 A 挂载专属 StorageService (SQLite 模拟)...')
  tenantA.plugin(StorageService, 'Tenant-A-SQLite')

  console.log('-> 为租户 B 挂载专属 StorageService (Redis 模拟)...')
  tenantB.plugin(StorageService, 'Tenant-B-Redis')

  console.log('\n-> 在两个租户上下文中分别运行相同的业务插件:')
  tenantA.plugin(appBusinessPlugin, 'Org_Alpha')
  tenantB.plugin(appBusinessPlugin, 'Org_Beta')

  await new Promise((r) => setTimeout(r, 200))

  console.log('\n-> 验证隔离完整性:')
  console.log('租户 A 的 Storage 数据:', tenantA.storage.dump())
  console.log('租户 B 的 Storage 数据:', tenantB.storage.dump())
  console.log('根上下文访问 storage:', root.storage ? '存在' : '未挂载 (互不污染！)')

  console.log('\n================================================================')
  console.log('2. ctx.intercept: 上下文级元数据与配置拦截覆盖')
  console.log('================================================================')
  const interceptedBranch = root.intercept('storage', { debug: true, maxRows: 100 })
  console.log('拦截树继承属性:', interceptedBranch[Context.intercept]?.storage)
}

main().catch(console.error)
