from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from cordis import Context, ServiceKey


@dataclass(frozen=True)
class ValidateOrderRequest:
    order_id: str
    customer: str
    amount: str


@dataclass(frozen=True)
class ImportRequest:
    path: Path


class StoreClosedError(RuntimeError):
    """The provider has been disposed; obtain the current service again."""


@dataclass(frozen=True)
class Order:
    """A validated order with nonempty names and positive signed-64-bit cents."""

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
    def validate(self, request: ValidateOrderRequest) -> Order:
        """Return a normalized order, or raise ValueError for an invalid row."""
        ...


class OrderStore(Protocol):
    """Upsert by order_id; all() returns an immutable snapshot sorted by id.

    Writes are visible immediately within a live provider instance. Calls after
    disposal raise StoreClosedError. Persistence across instances is a provider
    option, not part of this common contract.
    """

    def save(self, order: Order) -> None: ...

    def all(self) -> tuple[Order, ...]: ...


class OrderImporter(Protocol):
    def submit(self, request: ImportRequest) -> asyncio.Task[ImportReport]:
        """Start an owned import task and return data; callers present its result.

        Invalid rows appear in the report. File/CSV/storage errors fail the task;
        earlier successful rows remain committed. Cancelling a caller need not
        cancel this task: consumers may shield it while providers drain it.
        """
        ...


VALIDATOR = ServiceKey[OrderValidator]("orders.validator")
STORE = ServiceKey[OrderStore]("orders.store")
IMPORTER = ServiceKey[OrderImporter]("orders.importer")

EVENT_ORDERS_IMPORTED = "orders/imported"
EVENT_ORDERS_IMPORT_FAILED = "orders/import-failed"
EVENT_CONFIG_APPLIED = "config/applied"
EVENT_CONFIG_ERROR = "config/error"


class AppContext(Context):
    """Retain the context subclass for compatibility; new code can use Context."""

    validator = VALIDATOR
    store = STORE
    importer = IMPORTER
