import type { Context, Plugin } from 'cordis'
import path from 'node:path'
import type { ImportReport } from '../definitions'

/** 控制台日志插件: 订阅全局事件并结构化打印结果 */
export const consolePlugin: Plugin.Function<void> = (ctx: Context) => {
  ctx.on('orders/imported', (report: ImportReport) => {
    console.log(
      `[导入] ${path.basename(report.path)}: 成功 ${report.accepted} 行，拒绝 ${report.rejected.length} 行`
    )
    for (const issue of report.rejected) {
      console.log(`  第 ${issue.line} 行: ${issue.reason}`)
    }
  })

  ctx.on('orders/import-failed', (filePath: string, error: Error) => {
    console.log(`[导入失败] ${path.basename(filePath)}: ${error.message}`)
  })
}
