from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Generic, Never, TypeVar, overload

if TYPE_CHECKING:
    from .core import Context

T = TypeVar("T")


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
