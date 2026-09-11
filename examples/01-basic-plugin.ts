/**
 * 示例 1: 基础插件与生命周期
 *
 * 运行方式:
 *   npx tsx examples/01-basic-plugin.ts
 */

import { Context, Plugin } from 'cordis'

// 1. 箭头函数形式插件（无 prototype，作为函数插件执行，返回值自动作为 Dispose 回调）
const greetingPlugin = (ctx: Context, config: { name: string }) => {
  console.log(`[GreetingPlugin] 插件已加载，你好，${config.name}！`)

  // 注册清理回调 (Dispose)
  return () => {
    console.log(`[GreetingPlugin] 插件卸载，执行清理操作。`)
  }
}

// 2. 对象形式插件
const timerPlugin: Plugin.Object<{ interval: number }> = {
  name: 'timer-plugin',
  apply(ctx: Context, config: { interval: number }) {
    console.log(`[TimerPlugin] 启动，间隔: ${config.interval}ms`)

    // ctx.effect 会被当前 Fiber 严格追踪，插件卸载时自动执行清理
    ctx.effect(() => {
      const timer = setInterval(() => {
        console.log(`[TimerPlugin] 心跳 Tick...`)
      }, config.interval)

      return () => {
        clearInterval(timer)
        console.log(`[TimerPlugin] 心跳定时器已被自动清理！`)
      }
    })
  },
}

async function main() {
  const app = new Context()

  console.log('--- 挂载 GreetingPlugin ---')
  const fiber1 = await app.plugin(greetingPlugin, { name: 'Cordis Developer' })

  console.log('\n--- 挂载 TimerPlugin ---')
  const fiber2 = await app.plugin(timerPlugin, { interval: 300 })

  // 运行 1 秒，观察定时器心跳
  await new Promise((resolve) => setTimeout(resolve, 1000))

  console.log('\n--- 卸载 TimerPlugin ---')
  await fiber2.dispose()

  console.log('\n--- 卸载 GreetingPlugin ---')
  await fiber1.dispose()

  console.log('\n所有插件已安全卸载，无残留定时器与悬挂句柄！')
}

main().catch(console.error)
