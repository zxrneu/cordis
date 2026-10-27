from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .core import Context


@dataclass(frozen=True, slots=True)
class Accessor:
    """One dynamically exposed Context property."""

    get: Callable[[Context], object]
    set: Callable[[Context, object], None] | None = None
    get_for: Callable[[Context, object], object] | None = None
    set_for: Callable[[Context, object, object], None] | None = None
