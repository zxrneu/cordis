import { Context, Service } from 'cordis'
import { StoreClosedError, type OrderStore, type Order } from '../definitions'

/** 内存存储提供方: 基于 Map 存储，卸载时清空内存 */
export class MemoryStore extends Service implements OrderStore {
  private orders = new Map<string, Order>()
  private closed = false

  constructor(ctx: Context) {
    super(ctx, 'store')
  }

  protected [Service.init]() {
    return () => {
      this.close()
    }
  }

  save(order: Order): void {
    this.ensureOpen()
    this.orders.set(order.orderId, order)
  }

  all(): Order[] {
    this.ensureOpen()
    return Array.from(this.orders.values()).sort((a, b) => a.orderId.localeCompare(b.orderId))
  }

  private ensureOpen(): void {
    if (this.closed) throw new StoreClosedError('order store is closed')
  }

  close(): void {
    this.closed = true
    this.orders.clear()
  }
}
