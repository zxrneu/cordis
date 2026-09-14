import { Context } from 'cordis'
import fs from 'node:fs'
import path from 'node:path'
import os from 'node:os'
import { fileURLToPath } from 'node:url'
import { Application } from './application'
import { formatCents } from './money'
import type { ImportReport } from './definitions'

export * from './definitions'
export * from './money'
export * from './config'
export * from './application'
export * from './providers/validation'
export * from './providers/memory'
export * from './providers/sqlite'
export * from './providers/csv'
export * from './consumers/directory'
export * from './consumers/console'

/** 异步等待队列辅助类 */
class AsyncQueue<T> {
  private items: T[] = []
  private resolvers: ((value: T) => void)[] = []

  push(item: T) {
    if (this.resolvers.length > 0) {
      const resolve = this.resolvers.shift()!
      resolve(item)
    } else {
      this.items.push(item)
    }
  }

  async pop(timeoutMs = 5000): Promise<T> {
    if (this.items.length > 0) {
      return this.items.shift()!
    }
    return new Promise((resolve, reject) => {
      let timer: NodeJS.Timeout
      const resolver = (val: T) => {
        clearTimeout(timer)
        resolve(val)
      }

      timer = setTimeout(() => {
        const idx = this.resolvers.indexOf(resolver)
        if (idx !== -1) this.resolvers.splice(idx, 1)
        reject(new Error(`等待事件超时 (${timeoutMs}ms)`))
      }, timeoutMs)

      this.resolvers.push(resolver)
    })
  }
}

/** 完整复现 Python 版本的 --demo 自动化演示 (临时目录测试与清理) */
export async function runDemo() {
  const tempDir = fs.mkdtempSync(path.join(os.tmpdir(), 'cordis-orders-ts-'))
  const configPath = path.join(tempDir, 'config.toml')
  const incomingDir = path.join(tempDir, 'incoming')
  fs.mkdirSync(incomingDir, { recursive: true })

  const initialConfig = `[database]
provider = "sqlite"
path = "var/orders.sqlite3"

[validation]
max_amount = "50.00"

[importer]
directory = "incoming"
interval = 0.1

[logging]
enabled = true
`
  fs.writeFileSync(configPath, initialConfig, 'utf-8')

  const csvContent = `order_id,customer,amount
ORD-001,Alice,25.00
ORD-002,Bob,80.00
ORD-003,Carol,-5.00
`
  fs.writeFileSync(path.join(incomingDir, 'orders.csv'), csvContent, 'utf-8')

  console.log(`================================================================`)
  console.log(`Cordis CSV 订单导入实战演示 (TypeScript 模块化版)`)
  console.log(`临时测试目录: ${tempDir}`)
  console.log(`================================================================\n`)

  const app = new Context()
  const queue = new AsyncQueue<ImportReport>()
  app.on('orders/imported', (report) => queue.push(report))

  const application = new Application(app, configPath)
  await application.refresh()
  const stopWatch = application.watch(100)

  try {
    // 阶段 1: 初始扫描 ($50.00 上限)
    const report1 = await queue.pop()
    if (report1.accepted !== 1 || report1.rejected.length !== 2) {
      throw new Error(`阶段 1 断言失败: 期望 1 成功 2 拒绝，实际 ${report1.accepted} 成功 ${report1.rejected.length} 拒绝`)
    }

    // 阶段 2: 动态修改配置，将金额上限提升至 100.00
    console.log(`\n👉 [演示] 将配置文件中的金额上限从 50.00 改为 100.00...`)
    fs.writeFileSync(
      configPath,
      initialConfig.replace('max_amount = "50.00"', 'max_amount = "100.00"'),
      'utf-8'
    )

    const report2 = await queue.pop()
    if (report2.accepted !== 2 || report2.rejected.length !== 1) {
      throw new Error(`阶段 2 断言失败: 期望 2 成功 1 拒绝，实际 ${report2.accepted} 成功 ${report2.rejected.length} 拒绝`)
    }

    const rows = app.store.all()
    console.log(`\n📦 [数据库] 重复导入按订单号更新，当前共 ${rows.length} 条：`)
    for (const order of rows) {
      console.log(`  ${order.orderId} ${order.customer} ${formatCents(order.amountCents)}`)
    }

    // 阶段 3: 动态将 database.provider 从 sqlite 改成 memory
    console.log(`\n👉 [演示] 将 database.provider 从 sqlite 改成 memory (热替换存储提供方)...`)
    fs.writeFileSync(
      configPath,
      initialConfig
        .replace('max_amount = "50.00"', 'max_amount = "100.00"')
        .replace('provider = "sqlite"', 'provider = "memory"'),
      'utf-8'
    )

    const report3 = await queue.pop()
    if (report3.accepted !== 2) {
      throw new Error(`阶段 3 断言失败: 期望 2 成功，实际 ${report3.accepted}`)
    }
    const memRows = app.store.all()
    if (memRows.length !== 2) {
      throw new Error(`阶段 3 内存存储订单数量不符: 期望 2，实际 ${memRows.length}`)
    }

    console.log(`\n🎉 [替换成功] 内存提供方使用相同契约，导入与日志消费者保持原代码运行！`)
  } finally {
    // 阶段 4: 清理退出
    stopWatch()
    await application.dispose()
    fs.rmSync(tempDir, { recursive: true, force: true })
    console.log(`\n🛑 [退出] 配置监听、目录扫描和数据库连接已安全清理，临时文件已删除。`)
  }
}

/** 常驻运行模式 */
export async function runServer(configPath: string) {
  const absConfig = path.resolve(process.cwd(), configPath)
  if (!fs.existsSync(absConfig)) {
    console.error(`配置文件不存在: ${absConfig}`)
    process.exit(1)
  }

  const app = new Context()
  const application = new Application(app, absConfig)
  await application.refresh()
  application.watch(200)

  console.log(`正在监听 ${absConfig}，修改配置或 CSV 后保存；按 Ctrl+C 退出。`)
  process.on('SIGINT', async () => {
    console.log('\n正在停止...')
    await application.dispose()
    console.log('已安全退出。')
    process.exit(0)
  })
}

// 主入口分发
const currentFilePath = fileURLToPath(import.meta.url)
const isMain = process.argv[1] && (
  path.resolve(process.argv[1]) === currentFilePath ||
  path.resolve(process.argv[1]) === path.dirname(currentFilePath) ||
  path.resolve(process.argv[1]).includes('07-csv-order-importer')
)

if (isMain) {
  const args = process.argv.slice(2)
  if (args.includes('--help') || args.includes('-h')) {
    console.log(`Cordis CSV 订单导入示例用法:`)
    console.log(`  npx tsx examples/order-importer            # 运行全自动交互式演示 (在临时目录运行并清理)`)
    console.log(`  npx tsx examples/order-importer --serve    # 常驻监听模式 (默认使用 examples/order-importer/config.toml)`)
    console.log(`  npx tsx examples/order-importer --serve <path> # 使用指定配置文件常驻监听`)
    process.exit(0)
  }
  if (args.includes('--serve')) {
    const userPath = args.find((a) => !a.startsWith('--'))
    const defaultPath = path.resolve(path.dirname(currentFilePath), 'config.toml')
    const configArg = userPath || (fs.existsSync(defaultPath) ? defaultPath : 'config.toml')
    runServer(configArg).catch(console.error)
  } else {
    runDemo().catch(console.error)
  }
}
