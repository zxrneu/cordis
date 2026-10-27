"""Explicit, expression-only configuration interpolation (no statements or calls)."""

from __future__ import annotations

import ast
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .core import Context


@dataclass(frozen=True, slots=True)
class ConfigExpression:
    source: str

    def evaluate(self, ctx: Context) -> object:
        tree = ast.parse(self.source, mode="eval")
        allowed = (
            ast.Expression,
            ast.Constant,
            ast.Name,
            ast.Load,
            ast.Attribute,
            ast.Subscript,
            ast.Slice,
            ast.List,
            ast.Tuple,
            ast.Dict,
            ast.Set,
            ast.BinOp,
            ast.UnaryOp,
            ast.BoolOp,
            ast.Compare,
            ast.IfExp,
            ast.Add,
            ast.Sub,
            ast.Mult,
            ast.Div,
            ast.FloorDiv,
            ast.Mod,
            ast.USub,
            ast.UAdd,
            ast.Not,
            ast.And,
            ast.Or,
            ast.Eq,
            ast.NotEq,
            ast.Lt,
            ast.LtE,
            ast.Gt,
            ast.GtE,
            ast.In,
            ast.NotIn,
            ast.Is,
            ast.IsNot,
        )
        for node in ast.walk(tree):
            if not isinstance(node, allowed):
                raise ValueError(
                    f"unsupported configuration expression: {type(node).__name__}"
                )
            if isinstance(node, ast.Attribute) and node.attr.startswith("_"):
                raise ValueError("private attributes are not configuration inputs")
            if isinstance(node, ast.Name) and node.id.startswith("_"):
                raise ValueError("private names are not configuration inputs")
        return eval(
            compile(tree, "<cordis-config>", "eval"), {"__builtins__": {}}, _Names(ctx)
        )


class _Names(dict[str, Any]):
    def __init__(self, ctx: Context) -> None:
        super().__init__(ctx=ctx)
        self.ctx = ctx

    def __missing__(self, key: str) -> object:
        return getattr(self.ctx, key)


def interpolate(value: object, ctx: Context) -> object:
    if isinstance(value, ConfigExpression):
        return value.evaluate(ctx)
    if isinstance(value, Mapping):
        if set(value) == {"__expr__"}:
            source = value["__expr__"]
            if not isinstance(source, str):
                raise TypeError("configuration expression must be a string")
            return ConfigExpression(source).evaluate(ctx)
        return {key: interpolate(item, ctx) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        items = [interpolate(item, ctx) for item in value]
        return tuple(items) if isinstance(value, tuple) else items
    return value
