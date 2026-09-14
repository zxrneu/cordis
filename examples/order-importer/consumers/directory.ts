import type { Context, Plugin } from 'cordis'
import fs from 'node:fs'
import path from 'node:path'

export interface DirectoryWatcherConfig {
  directory: string
  interval: number // 轮询间隔 (秒)
}

/** 目录监听插件: 依赖注入 'importer'，通过 ctx.effect 受管管理轮询定时器 */
export const directoryWatcherPlugin: Plugin.Object<DirectoryWatcherConfig> = {
  name: 'directory-watcher',
  inject: ['importer'],
  apply(ctx: Context, config: DirectoryWatcherConfig) {
    fs.mkdirSync(config.directory, { recursive: true })
    const seen = new Map<string, string>()

    ctx.effect(() => {
      let active = true

      const scan = async () => {
        if (!active) return
        try {
          const files = (await fs.promises.readdir(config.directory))
            .filter((f) => f.endsWith('.csv'))
            .sort()

          for (const file of files) {
            if (!active) break
            const fullPath = path.join(config.directory, file)
            let stat: fs.Stats
            try {
              stat = await fs.promises.stat(fullPath)
            } catch {
              continue
            }
            const signature = `${stat.mtimeMs}:${stat.size}`
            if (seen.get(fullPath) === signature) continue
            seen.set(fullPath, signature)

            try {
              const report = await ctx.importer.submit({ path: fullPath })
              ctx.emit('orders/imported', report)
            } catch (err: any) {
              ctx.emit('orders/import-failed', fullPath, err)
            }
          }
        } catch {
          // 忽略目录扫描临时读取异常
        }
      }

      scan()
      const timer = setInterval(scan, Math.max(config.interval * 1000, 50))

      return () => {
        active = false
        clearInterval(timer)
      }
    })
  },
}
