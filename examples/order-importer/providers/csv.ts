import { Context, Service } from 'cordis'
import fs from 'node:fs'
import path from 'node:path'
import type { OrderImporter, ImportRequest, ImportReport, RowIssue } from '../definitions'

/** CSV 导入服务提供方: 声明式注入 ['store', 'validator'] */
export class CsvImporter extends Service implements OrderImporter {
  static inject = ['store', 'validator']

  constructor(ctx: Context) {
    super(ctx, 'importer')
  }

  async submit(request: ImportRequest): Promise<ImportReport> {
    const filePath = request.path
    const content = await fs.promises.readFile(filePath, 'utf-8')
    const lines = content.split(/\r?\n/).filter((line) => line.trim().length > 0)

    if (lines.length === 0) {
      throw new Error(`${path.basename(filePath)}: 文件内容为空`)
    }

    const header = lines[0].split(',').map((s) => s.trim())
    if (header[0] !== 'order_id' || header[1] !== 'customer' || header[2] !== 'amount') {
      throw new Error(`${path.basename(filePath)}: 表头必须是 order_id,customer,amount`)
    }

    let accepted = 0
    const rejected: RowIssue[] = []

    for (let i = 1; i < lines.length; i++) {
      const lineNum = i + 1
      const cols = lines[i].split(',').map((s) => s.trim())
      if (cols.length !== 3) {
        rejected.push({ line: lineNum, reason: '每行必须有三列' })
        continue
      }
      try {
        const order = this.ctx.validator.validate({
          orderId: cols[0],
          customer: cols[1],
          amount: cols[2],
        })
        this.ctx.store.save(order)
        accepted++
      } catch (err: any) {
        rejected.push({ line: lineNum, reason: err.message })
      }
      // 让出事件循环，让生命周期钩子和并发任务能交替处理
      await new Promise((resolve) => setImmediate(resolve))
    }

    return {
      path: filePath,
      accepted,
      rejected,
    }
  }
}
