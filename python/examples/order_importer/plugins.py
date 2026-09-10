from __future__ import annotations

import asyncio
import csv
import sqlite3
from collections.abc import Iterator
from pathlib import Path

from cordis import Cleanup, plugin

from .config import (
    DatabaseConfig,
    ImporterConfig,
    ValidationConfig,
    format_cents,
    parse_cents,
)
from .contracts import (
    IMPORTER,
    STORE,
    VALIDATOR,
    AppContext,
    ImportReport,
    Order,
    RowIssue,
)


class SQLiteStore:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def save(self, order: Order) -> None:
        with self.connection:
            self.connection.execute(
                "INSERT INTO orders VALUES (?, ?, ?) "
                "ON CONFLICT(order_id) DO UPDATE SET "
                "customer = excluded.customer, amount_cents = excluded.amount_cents",
                (order.order_id, order.customer, order.amount_cents),
            )

    def all(self) -> tuple[Order, ...]:
        return tuple(
            Order(str(row[0]), str(row[1]), int(row[2]))
            for row in self.connection.execute(
                "SELECT order_id, customer, amount_cents FROM orders ORDER BY order_id"
            )
        )


@plugin(provide=[STORE])
def database(ctx: AppContext, config: DatabaseConfig) -> Iterator[Cleanup]:
    config.path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(config.path)
    yield connection.close
    with connection:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS orders ("
            "order_id TEXT PRIMARY KEY, customer TEXT NOT NULL, "
            "amount_cents INTEGER NOT NULL)"
        )
    ctx.provide(STORE, SQLiteStore(connection))


class AmountValidator:
    def __init__(self, config: ValidationConfig) -> None:
        self.config = config

    def validate(self, order_id: str, customer: str, amount: str) -> Order:
        order_id, customer = order_id.strip(), customer.strip()
        if not order_id or not customer:
            raise ValueError("订单号和客户不能为空")
        cents = parse_cents(amount)
        if cents > self.config.max_amount_cents:
            raise ValueError(
                f"金额超过上限 {format_cents(self.config.max_amount_cents)}"
            )
        return Order(order_id, customer, cents)


@plugin(provide=[VALIDATOR])
def validation(ctx: AppContext, config: ValidationConfig) -> None:
    ctx.provide(VALIDATOR, AmountValidator(config))


class DirectoryImporter:
    def __init__(self, ctx: AppContext, config: ImporterConfig) -> None:
        self.ctx = ctx
        self.config = config

    def submit(self, path: Path) -> asyncio.Task[ImportReport]:
        return self.ctx.spawn(
            self._import(path),
            policy="drain",
            timeout=5,
            name=f"import:{path.name}",
        )

    async def _import(self, path: Path) -> ImportReport:
        accepted = 0
        issues: list[RowIssue] = []
        with path.open(newline="", encoding="utf-8-sig") as stream:
            reader = csv.reader(stream, strict=True)
            if next(reader, None) != ["order_id", "customer", "amount"]:
                raise ValueError(f"{path.name}: 表头必须是 order_id,customer,amount")
            for row in reader:
                try:
                    if len(row) != 3:
                        raise ValueError("每行必须有三列")
                    order = self.ctx.validator.validate(row[0], row[1], row[2])
                except ValueError as error:
                    issues.append(RowIssue(reader.line_num, str(error)))
                else:
                    self.ctx.store.save(order)
                    accepted += 1
                # Yield between rows so config changes and teardown can progress.
                await asyncio.sleep(0)
        report = ImportReport(path, accepted, tuple(issues))
        self.ctx.emit("orders/imported", report)
        return report

    async def watch(self) -> None:
        seen: dict[Path, tuple[int, int]] = {}
        while True:
            for path in sorted(self.config.directory.glob("*.csv")):
                try:
                    stat = path.stat()
                except FileNotFoundError:
                    continue
                signature = (stat.st_mtime_ns, stat.st_size)
                if seen.get(path) == signature:
                    continue
                seen[path] = signature
                try:
                    # Cancelling the scanner must not cancel a draining import.
                    await asyncio.shield(self.submit(path))
                except Exception as error:
                    self.ctx.emit("orders/import-failed", path, error)
            await asyncio.sleep(self.config.interval)


@plugin(inject=[STORE, VALIDATOR], provide=[IMPORTER])
def importer(ctx: AppContext, config: ImporterConfig) -> None:
    config.directory.mkdir(parents=True, exist_ok=True)
    service = DirectoryImporter(ctx, config)
    ctx.provide(IMPORTER, service)
    ctx.spawn(service.watch(), policy="cancel", name="orders:scan")


@plugin
def console(ctx: AppContext, _config: None) -> None:
    def completed(report: ImportReport) -> None:
        print(
            f"[导入] {report.path.name}: "
            f"成功 {report.accepted} 行，拒绝 {len(report.rejected)} 行",
            flush=True,
        )
        for issue in report.rejected:
            print(f"  第 {issue.line} 行: {issue.reason}", flush=True)

    def failed(path: Path, error: Exception) -> None:
        print(f"[导入失败] {path.name}: {error}", flush=True)

    ctx.on("orders/imported", completed)
    ctx.on("orders/import-failed", failed)
