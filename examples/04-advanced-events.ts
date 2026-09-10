/**
 * 示例 4: 高级事件派发机制全解 (emit / parallel / serial / bail / waterfall)
 * 
 * 运行方式:
 *   npx tsx examples/04-advanced-events.ts
 */

import { Context } from 'cordis'

// 1. 类型扩充 (Module Augmentation)
// 强类型声明事件名及其形参/返回值
declare module 'cordis' {
  interface Events {
    'notice/broadcast'(message: string): void
    'task/process'(id: number): Promise<void>
    'auth/check'(token: string): boolean | string
    'middleware/pipeline'(content: string, next: () => string): string
  }
}

async function main() {
  const app = new Context()

  console.log('================================================================')
  console.log('1. emit: 异步广播 (Fire-and-forget)')
  console.log('================================================================')
  app.on('notice/broadcast', (msg) => {
    console.log(`  [Listener A] 收到广播: ${msg}`)
  })
  app.on('notice/broadcast', (msg) => {
    console.log(`  [Listener B] 收到广播: ${msg}`)
  })
  app.emit('notice/broadcast', 'Hello Cordis Event Bus!')

  console.log('\n================================================================')
  console.log('2. parallel: 并发等待 (Fan-out / Fan-in)')
  console.log('================================================================')
  app.on('task/process', async (id) => {
    await new Promise((r) => setTimeout(r, 200))
    console.log(`  [Worker 1] 处理任务 #${id} 完成 (耗时 200ms)`)
  })
  app.on('task/process', async (id) => {
    await new Promise((r) => setTimeout(r, 100))
    console.log(`  [Worker 2] 处理任务 #${id} 完成 (耗时 100ms)`)
  })

  console.log('  -> 并发启动所有 Worker...')
  const start = Date.now()
  await app.parallel('task/process', 1001)
  console.log(`  -> parallel 全部完成，总耗时约 ${Date.now() - start}ms (并发而非累加！)`)

  console.log('\n================================================================')
  console.log('3. bail: 同步短路熔断 (常用于权限校验与路由拦截)')
  console.log('================================================================')
  // 监听器 1: 检查是否空 token
  app.on('auth/check', (token) => {
    if (!token) return 'Token 不能为空'
    return undefined // 返回 undefined 表示通过，放行到下一个监听器
  })

  // 监听器 2: 检查是否被封禁
  app.on('auth/check', (token) => {
    if (token === 'banned_123') return '用户已被系统封禁'
    return undefined
  })

  // 监听器 3: 正确放行
  app.on('auth/check', (token) => {
    if (token === 'admin_secret') return true
    return undefined
  })

  console.log('  Token: ""            -> 校验结果:', app.bail('auth/check', ''))
  console.log('  Token: "banned_123"  -> 校验结果:', app.bail('auth/check', 'banned_123'))
  console.log('  Token: "admin_secret"-> 校验结果:', app.bail('auth/check', 'admin_secret'))
  console.log('  Token: "unknown"     -> 校验结果:', app.bail('auth/check', 'unknown'))

  console.log('\n================================================================')
  console.log('4. waterfall: 洋葱圈管道流 (Koa/Express 式中间件流水线)')
  console.log('================================================================')
  // 中间件 1: 去除首尾空白
  app.on('middleware/pipeline', (data, next) => {
    console.log('    [Step 1 -> 进入] 执行 trim')
    const result = next().trim()
    console.log('    [Step 1 <- 离开] trim 完成')
    return result
  })

  // 中间件 2: 包装外层标签
  app.on('middleware/pipeline', (data, next) => {
    console.log('    [Step 2 -> 进入] 添加外层前缀')
    const result = `[Prefix] => ${next()}`
    console.log('    [Step 2 <- 离开] 前缀添加完成')
    return result
  })

  // 中间件 3: 转换大写
  app.on('middleware/pipeline', (data, next) => {
    console.log('    [Step 3 -> 进入] 转换为大写')
    const result = next().toUpperCase()
    console.log('    [Step 3 <- 离开] 大写转换完成')
    return result
  })

  const rawInput = '   cordis event pipeline demo   '
  console.log(`  输入原始文本: "${rawInput}"`)
  // 最后一个参数是兜底内核函数 (inner target)
  const output = app.waterfall('middleware/pipeline', rawInput, () => rawInput)
  console.log(`  最终输出结果: "${output}"`)
}

main().catch(console.error)
