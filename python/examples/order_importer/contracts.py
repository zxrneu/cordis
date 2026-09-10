from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from cordis import Context, ServiceKey


@dataclass(frozen=True)
class Order:
    order_id: str
    customer: str
    amount_cents: int


@dataclass(frozen=True)
class RowIssue:
    line: int
    reason: str


@dataclass(frozen=True)
class ImportReport:
    path: Path
    accepted: int
    rejected: tuple[RowIssue, ...]


class OrderValidator(Protocol):
    def validate(self, order_id: str, customer: str, amount: str) -> Order: ...


class OrderStore(Protocol):
    def save(self, order: Order) -> None: ...

    def all(self) -> tuple[Order, ...]: ...


class OrderImporter(Protocol):
    def submit(self, path: Path) -> asyncio.Task[ImportReport]: ...


VALIDATOR = ServiceKey[OrderValidator]("orders.validator")
STORE = ServiceKey[OrderStore]("orders.store")
IMPORTER = ServiceKey[OrderImporter]("orders.importer")


class AppContext(Context):
    validator = VALIDATOR
    store = STORE
    importer = IMPORTER
