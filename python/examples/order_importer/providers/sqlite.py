from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from cordis import Cleanup, plugin

from ..definitions import STORE, AppContext, Order, StoreClosedError


@dataclass(frozen=True)
class DatabaseConfig:
    path: Path


class SQLiteStore:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection
        self.closed = False

    def save(self, order: Order) -> None:
        self._ensure_open()
        with self.connection:
            self.connection.execute(
                "INSERT INTO orders VALUES (?, ?, ?) "
                "ON CONFLICT(order_id) DO UPDATE SET "
                "customer = excluded.customer, amount_cents = excluded.amount_cents",
                (order.order_id, order.customer, order.amount_cents),
            )

    def all(self) -> tuple[Order, ...]:
        self._ensure_open()
        return tuple(
            Order(str(row[0]), str(row[1]), int(row[2]))
            for row in self.connection.execute(
                "SELECT order_id, customer, amount_cents FROM orders ORDER BY order_id"
            )
        )

    def _ensure_open(self) -> None:
        if self.closed:
            raise StoreClosedError("order store is closed")

    def close(self) -> None:
        if not self.closed:
            self.connection.close()
            self.closed = True


@plugin(provide=[STORE])
def database(ctx: AppContext, config: DatabaseConfig) -> Iterator[Cleanup]:
    config.path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(config.path)
    store = SQLiteStore(connection)
    yield store.close
    with connection:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS orders ("
            "order_id TEXT PRIMARY KEY, customer TEXT NOT NULL, "
            "amount_cents INTEGER NOT NULL)"
        )
    ctx.provide(STORE, store)
