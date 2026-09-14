import type { AppConfig } from './config'

export class StoreClosedError extends Error {
  constructor(message = 'Order store is closed') {
    super(message)
    this.name = 'StoreClosedError'
  }
}

/** 校验完成的规范化订单模型 (金额使用整数分存储，杜绝浮点数精度失真) */
export interface Order {
  orderId: string
  customer: string
  amountCents: number
}

/** 单行校验问题记录 */
export interface RowIssue {
  line: number
  reason: string
}

/** 单次 CSV 导入执行报告 */
export interface ImportReport {
  path: string
  accepted: number
  rejected: RowIssue[]
}

/** 订单行校验请求数据 */
export interface ValidateOrderRequest {
  orderId: string
  customer: string
  amount: string
}

/** 导入请求 */
export interface ImportRequest {
  path: string
}

/** 订单校验服务契约 */
export interface OrderValidator {
  validate(request: ValidateOrderRequest): Order
}

/** 订单存储服务契约 (支持按 order_id upsert，并提供有序快照) */
export interface OrderStore {
  save(order: Order): void
  all(): Order[]
}

/** 订单导入服务契约 */
export interface OrderImporter {
  submit(request: ImportRequest): Promise<ImportReport>
}

// 模块扩充: 让 TypeScript 编译器获得强类型智能补全与类型检查
declare module 'cordis' {
  interface Context {
    validator: OrderValidator
    store: OrderStore
    importer: OrderImporter
  }

  interface Events {
    'orders/imported'(report: ImportReport): void
    'orders/import-failed'(filePath: string, error: Error): void
    'config/applied'(config: AppConfig): void
    'config/error'(error: Error): void
  }
}
