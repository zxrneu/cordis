import { Context, Service } from 'cordis'
import { DatabaseSync } from 'node:sqlite'
import fs from 'node:fs'
import path from 'node:path'
import { StoreClosedError, type OrderStore, type Order } from '../definitions'

export interface SQLiteDatabaseConfig {
  path: string
}

/** SQLite 存储提供方: 基于 Node 原生 node:sqlite (DatabaseSync) */
export class SQLiteStore extends Service implements OrderStore {
  private db: DatabaseSync | null = null
  private closed = false

  constructor(ctx: Context, public config: SQLiteDatabaseConfig) {
    super(ctx, 'store')
  }

  protected [Service.init]() {
    fs.mkdirSync(path.dirname(this.config.path), { recursive: true })
    const db = new DatabaseSync(this.config.path)
    this.db = db
    db.exec(`
      CREATE TABLE IF NOT EXISTS orders (
        order_id TEXT PRIMARY KEY,
        customer TEXT NOT NULL,
        amount_cents INTEGER NOT NULL
      )
    `)

    return () => {
      this.close()
    }
  }

  save(order: Order): void {
    this.ensureOpen()
    const stmt = this.db!.prepare(`
      INSERT INTO orders (order_id, customer, amount_cents)
      VALUES (?, ?, ?)
      ON CONFLICT(order_id) DO UPDATE SET
        customer = excluded.customer,
        amount_cents = excluded.amount_cents
    `)
    stmt.run(order.orderId, order.customer, order.amountCents)
  }

  all(): Order[] {
    this.ensureOpen()
    const stmt = this.db!.prepare(`
      SELECT order_id AS orderId, customer, amount_cents AS amountCents
      FROM orders
      ORDER BY order_id
    `)
    const rows = stmt.all() as any[]
    return rows.map((row) => ({
      orderId: String(row.orderId),
      customer: String(row.customer),
      amountCents: Number(row.amountCents),
    }))
  }

  private ensureOpen(): void {
    if (this.closed || !this.db) throw new StoreClosedError('order store is closed')
  }

  close(): void {
    if (!this.closed) {
      this.closed = true
      this.db?.close()
      this.db = null
    }
  }
}
