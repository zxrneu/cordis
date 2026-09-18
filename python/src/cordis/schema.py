from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from typing import Protocol, TypeAlias, TypeVar, cast


T = TypeVar("T")
T_co = TypeVar("T_co", covariant=True)


class Schema(Protocol[T_co]):
    def validate(self, value: object) -> T_co | Awaitable[T_co]: ...


class PythonSchema(Protocol[T_co]):
    def validate_python(self, value: object) -> T_co | Awaitable[T_co]: ...


class ModelSchema(Protocol[T_co]):
    def model_validate(self, value: object) -> T_co | Awaitable[T_co]: ...


SchemaLike: TypeAlias = (
    Schema[T]
    | PythonSchema[T]
    | ModelSchema[T]
    | Callable[[object], T | Awaitable[T]]
    | type[T]
)


async def decode_schema(schema: SchemaLike[T], value: object) -> T:
    """Decode with a callable, a validate protocol, or a Pydantic-style schema."""

    decoder: Callable[[object], object]
    if callable(schema) and not isinstance(schema, type):
        decoder = schema
    elif callable(method := getattr(schema, "validate_python", None)):
        decoder = method
    elif callable(method := getattr(schema, "model_validate", None)):
        decoder = method
    elif callable(method := getattr(schema, "validate", None)):
        decoder = method
    elif isinstance(schema, type):
        decoder = cast(Callable[[object], object], schema)
    else:
        raise TypeError("schema must be callable or expose a validation method")
    result = decoder(value)
    if inspect.isawaitable(result):
        result = await result
    return cast(T, result)


def schema_decoder(schema: SchemaLike[T]) -> Callable[[object], Awaitable[T]]:
    async def decode(value: object) -> T:
        return await decode_schema(schema, value)

    return decode
