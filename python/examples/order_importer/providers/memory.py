from contextlib import closing
from dataclasses import dataclass

from cordis import Context, plugin

from ..definitions import STORE, Order, StoreClosedError


@dataclass(frozen=True)
class MemoryConfig:
    """No connection parameters are needed for an in-memory provider."""


class MemoryStore:
    def __init__(self) -> None:
        self._orders: dict[str, Order] = {}
        self._closed = False

    def save(self, order: Order) -> None:
        self._ensure_open()
        self._orders[order.order_id] = order

    def all(self) -> tuple[Order, ...]:
        self._ensure_open()
        return tuple(self._orders[key] for key in sorted(self._orders))

    def _ensure_open(self) -> None:
        if self._closed:
            raise StoreClosedError("order store is closed")

    def close(self) -> None:
        self._closed = True
        self._orders.clear()


@plugin(provide=[STORE])
def memory(ctx: Context, _config: MemoryConfig) -> None:
    store = ctx.enter_context(closing(MemoryStore()))
    ctx.provide(STORE, store)
