from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterable
from typing import TYPE_CHECKING, Any, Generic, Protocol, TypeVar, cast

if TYPE_CHECKING:
    from .core import Component, Context, Fiber, FiberView


ContextCov = TypeVar("ContextCov", bound="Context", covariant=True)


class _RegistryRuntime(Protocol):
    fibers: list[Fiber[Any, Any]]


class ComponentRegistry(Generic[ContextCov]):
    """Read and operate on mounted component instances without owning them."""

    def __init__(self, runtime: _RegistryRuntime) -> None:
        self._runtime = runtime

    @property
    def fibers(self) -> tuple[FiberView[ContextCov], ...]:
        return cast("tuple[FiberView[ContextCov], ...]", tuple(self._runtime.fibers))

    def find(
        self,
        component: Component[Any, Any] | Callable[..., object] | None = None,
        *,
        name: str | None = None,
    ) -> tuple[FiberView[ContextCov], ...]:
        def matches(fiber: Fiber[Any, Any]) -> bool:
            spec = fiber.component
            if spec is None:
                return False
            if (
                component is not None
                and spec is not component
                and spec.apply is not component
            ):
                return False
            return name is None or fiber.name == name

        return cast(
            "tuple[FiberView[ContextCov], ...]",
            tuple(fiber for fiber in self._runtime.fibers if matches(fiber)),
        )

    def _mutable(
        self,
        component: Component[Any, Any] | Callable[..., object] | None,
        name: str | None,
    ) -> Iterable[Fiber[Any, Any]]:
        selected = set(self.find(component, name=name))
        return tuple(fiber for fiber in self._runtime.fibers if fiber in selected)

    async def restart(
        self,
        component: Component[Any, Any] | Callable[..., object] | None = None,
        *,
        name: str | None = None,
    ) -> None:
        await asyncio.gather(
            *(fiber.restart() for fiber in self._mutable(component, name))
        )

    async def dispose(
        self,
        component: Component[Any, Any] | Callable[..., object] | None = None,
        *,
        name: str | None = None,
    ) -> None:
        results = await asyncio.gather(
            *(fiber.dispose() for fiber in self._mutable(component, name)),
            return_exceptions=True,
        )
        errors = [result for result in results if isinstance(result, Exception)]
        if errors:
            raise ExceptionGroup("component registry cleanup failed", errors)
