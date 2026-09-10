/**
 * 示例 3: 响应式依赖注入与自动自愈机制 (Reactive Injection & Self-Healing)
 * 
 * 演示场景:
 * 1. 乱序挂载: 先挂载消费者 (Consumer)，因缺少依赖处于 Pending
 * 2. 依赖上线: 挂载提供者 (Provider)，消费者感知并自动激活
 * 3. 依赖下线: 提供者卸载，消费者自动回退并清理所有已申请副作用 (零泄漏)
 * 4. 依赖重现: 提供者换配置重新上线，消费者自动自愈并重新激活
 * 
 * 运行方式:
 *   npx tsx examples/03-reactive-injection.ts
 */

import { Context, Service } from 'cordis'

declare module 'cordis' {
  interface Context {
    cache: CacheService
  }
}

// 1. 服务提供方
class CacheService extends Service {
  public store = new Map<string, string>()

  constructor(ctx: Context) {
    super(ctx, 'cache')
  }

  async [Service.init]() {
    console.log(`  🔌 [CacheService] 缓存服务已上线`)
    return () => {
      this.store.clear()
      console.log(`  🛑 [CacheService] 缓存服务已下线并清理内存`)
    }
  }

  set(k: string, v: string) {
    this.store.set(k, v)
  }

  get(k: string) {
    return this.store.get(k)
  }
}

// 2. 消费者插件 (声明 inject 依赖 'cache')
const consumerPlugin = {
  name: 'cache-consumer',
  inject: ['cache'], // 显式声明依赖 cache 服务
  apply(ctx: Context) {
    console.log(`  🚀 [Consumer] 依赖满足！插件激活，当前缓存状态: ${ctx.cache ? '在线' : '离线'}`)

    // 注册受管定时器
    ctx.effect(() => {
      console.log(`  ⏰ [Consumer] 启动心跳写入任务...`)
      const timer = setInterval(() => {
        ctx.cache.set('ping', new Date().toISOString())
        console.log(`  💓 [Consumer] 心跳已写入缓存: ping -> ${ctx.cache.get('ping')}`)
      }, 400)

      return () => {
        clearInterval(timer)
        console.log(`  🧹 [Consumer] 心跳定时器已被自动撤销 (无内存泄漏！)`)
      }
    })
  },
}

async function main() {
  const app = new Context()

  console.log('【阶段 1】乱序挂载消费者 (此时 CacheService 尚不存在)...')
  const consumerFiber = app.plugin(consumerPlugin)
  console.log(`👉 消费者当前状态: 挂起等待中 (Pending)`)

  await new Promise((r) => setTimeout(r, 600))

  console.log('\n【阶段 2】缓存服务上线 (Reactive Activation)...')
  const cacheFiber = app.plugin(CacheService)
  await cacheFiber

  // 观察心跳执行
  await new Promise((r) => setTimeout(r, 1000))

  console.log('\n【阶段 3】缓存服务下线 (模拟故障或热卸载)...')
  await cacheFiber.dispose()
  console.log(`👉 缓存已下线，等待 600ms 验证消费者是否彻底静止...`)

  await new Promise((r) => setTimeout(r, 600))

  console.log('\n【阶段 4】缓存服务重新上线 (自愈 Self-Healing 验证)...')
  const newCacheFiber = app.plugin(CacheService)
  await newCacheFiber

  await new Promise((r) => setTimeout(r, 900))

  console.log('\n【阶段 5】宿主完全退出并清理...')
  await consumerFiber.dispose()
  await newCacheFiber.dispose()
  console.log('全部卸载完成！')
}

main().catch(console.error)
