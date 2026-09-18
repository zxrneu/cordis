from __future__ import annotations

from dataclasses import dataclass
from typing import (
    TYPE_CHECKING,
    Generic,
    Never,
    TypeVar,
    TypeVarTuple,
    Unpack,
    overload,
)

if TYPE_CHECKING:
    from .core import Context

T = TypeVar("T")
EventArgsT = TypeVarTuple("EventArgsT")
RequestT = TypeVar("RequestT")
ResultT = TypeVar("ResultT")


@dataclass(frozen=True, eq=False)
class EventKey(Generic[Unpack[EventArgsT]]):
    """Invariant argument contract; dispatch uses key identity, not its label."""

    name: str

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name:
            raise ValueError("an event key needs a non-empty string name")

    def __str__(self) -> str:
        return self.name


@dataclass(frozen=True, eq=False)
class QueryKey(Generic[RequestT, ResultT]):
    """Invariant request/result contract for one handler per runtime and key."""

    name: str

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name:
            raise ValueError("a query key needs a non-empty string name")

    def __str__(self) -> str:
        return self.name


@dataclass(frozen=True)
class ServiceKey(Generic[T]):
    """A service name carrying its invariant value type.

    Define keys once in a shared contracts module. Names share the existing
    string namespace, so declarations, isolation, and configuration still work.
    Keys can also be used as read-only descriptors on Context subclasses.
    """

    name: str

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name:
            raise ValueError("a service key needs a non-empty string name")

    @overload
    def __get__(
        self, instance: None, owner: type[Context] | None = None
    ) -> ServiceKey[T]: ...

    @overload
    def __get__(self, instance: Context, owner: type[Context] | None = None) -> T: ...

    def __get__(
        self, instance: Context | None, owner: type[Context] | None = None
    ) -> T | ServiceKey[T]:
        return self if instance is None else instance.service(self)

    def __set__(self, instance: Context, value: Never) -> Never:
        raise AttributeError("service accessors are read-only; use ctx.set(key, value)")
