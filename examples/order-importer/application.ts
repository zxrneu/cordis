import { Context, Fiber } from 'cordis'
import fs from 'node:fs'
import path from 'node:path'
import { AppConfig, parseConfig } from './config'
import { formatCents, parseCents } from './money'
import { consolePlugin } from './consumers/console'
import { SQLiteStore } from './providers/sqlite'
import { MemoryStore } from './providers/memory'
import { AmountValidator } from './providers/validation'
import { CsvImporter } from './providers/csv'
import { directoryWatcherPlugin } from './consumers/directory'

export class Application {
  public config?: AppConfig
  private observedContent?: string
  private storeFiber?: Fiber
  private validatorFiber?: Fiber
  private importerFiber?: Fiber
  private watcherFiber?: Fiber
  private consoleFiber?: Fiber

  constructor(public ctx: Context, public configPath: string) {}

  async refresh(): Promise<boolean> {
    const content = await fs.promises.readFile(this.configPath, 'utf-8')
    if (content === this.observedContent) return false
    this.observedContent = content

    const nextConfig = parseConfig(content, path.dirname(this.configPath))
    await this.apply(nextConfig)
    return true
  }

  async apply(nextConfig: AppConfig) {
    const prevConfig = this.config
    this.config = nextConfig

    // 1. 日志插件 (开启 / 关闭)
    if (nextConfig.logging.enabled) {
      if (!this.consoleFiber) {
        this.consoleFiber = this.ctx.plugin(consolePlugin)
      }
    } else {
      if (this.consoleFiber) {
        await this.consoleFiber.dispose()
        this.consoleFiber = undefined
      }
    }

    // 2. 存储服务 (SQLite <-> Memory 热插拔切换)
    const storeChanged =
      !prevConfig ||
      prevConfig.database.provider !== nextConfig.database.provider ||
      (nextConfig.database.provider === 'sqlite' &&
        prevConfig.database.provider === 'sqlite' &&
        prevConfig.database.path !== nextConfig.database.path)

    if (storeChanged) {
      if (this.storeFiber) {
        await this.storeFiber.dispose()
        this.storeFiber = undefined
      }
      if (nextConfig.database.provider === 'sqlite') {
        this.storeFiber = await this.ctx.plugin(SQLiteStore, { path: nextConfig.database.path })
      } else {
        this.storeFiber = await this.ctx.plugin(MemoryStore)
      }
      await this.storeFiber
    }

    // 3. 校验服务 (金额上限变更通过 fiber.update 响应式热更新)
    const maxCents = parseCents(nextConfig.validation.maxAmount)
    if (!this.validatorFiber) {
      this.validatorFiber = await this.ctx.plugin(AmountValidator, { maxAmountCents: maxCents })
    } else if (prevConfig?.validation.maxAmount !== nextConfig.validation.maxAmount) {
      await this.validatorFiber.update({ maxAmountCents: maxCents })
    }

    // 4. CSV 导入服务 (声明依赖 store 与 validator)
    if (!this.importerFiber) {
      this.importerFiber = await this.ctx.plugin(CsvImporter)
    }

    // 5. 目录监听扫描插件 (声明依赖 importer)
    const watcherChanged =
      !prevConfig ||
      prevConfig.importer.directory !== nextConfig.importer.directory ||
      prevConfig.importer.interval !== nextConfig.importer.interval

    if (watcherChanged) {
      if (this.watcherFiber) {
        await this.watcherFiber.dispose()
        this.watcherFiber = undefined
      }
      this.watcherFiber = await this.ctx.plugin(directoryWatcherPlugin, {
        directory: nextConfig.importer.directory,
        interval: nextConfig.importer.interval,
      })
    }

    console.log(
      `[配置已更新] 金额上限 ${formatCents(maxCents)}，扫描间隔 ${nextConfig.importer.interval}s，日志 ${nextConfig.logging.enabled}，存储 ${nextConfig.database.provider}`
    )
    this.ctx.emit('config/applied', nextConfig)
  }

  /** 启动定时监控配置文件变动 */
  watch(intervalMs = 200): () => void {
    let stopped = false
    let lastError: string | null = null

    const timer = setInterval(async () => {
      if (stopped) return
      try {
        await this.refresh()
        lastError = null
      } catch (err: any) {
        const msg = String(err.message)
        if (msg !== lastError) {
          console.log(`[配置错误] ${msg}；等待下一次保存`)
          this.ctx.emit('config/error', err)
          lastError = msg
        }
      }
    }, intervalMs)

    return () => {
      stopped = true
      clearInterval(timer)
    }
  }

  /** 完整停止与释放所有挂载的插件 Fiber */
  async dispose() {
    await this.watcherFiber?.dispose()
    await this.importerFiber?.dispose()
    await this.validatorFiber?.dispose()
    await this.storeFiber?.dispose()
    await this.consoleFiber?.dispose()
  }
}
