import { Context, Service } from 'cordis'
import type { OrderValidator, ValidateOrderRequest, Order } from '../definitions'
import { parseCents, formatCents } from '../money'

export interface ValidationConfig {
  maxAmountCents: number
}

/** 校验服务提供方: 根据配置校验非空以及金额上限 */
export class AmountValidator extends Service implements OrderValidator {
  constructor(ctx: Context, public config: ValidationConfig) {
    super(ctx, 'validator')
  }

  validate(request: ValidateOrderRequest): Order {
    const orderId = request.orderId.trim()
    const customer = request.customer.trim()
    if (!orderId || !customer) {
      throw new Error('订单号和客户不能为空')
    }
    const cents = parseCents(request.amount)
    if (cents > this.config.maxAmountCents) {
      throw new Error(`金额超过上限 ${formatCents(this.config.maxAmountCents)}`)
    }
    return { orderId, customer, amountCents: cents }
  }
}
