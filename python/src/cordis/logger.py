from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .core import Context, EffectHandle


def _normalize_level(level: object) -> int | None:
    if level is None:
        return None
    if isinstance(level, int):
        return level
    if isinstance(level, str):
        mapping = logging.getLevelNamesMapping()
        canonical = level.strip().upper()
        if canonical in mapping:
            return mapping[canonical]
        try:
            return int(level)
        except ValueError:
            pass
    raise ValueError(f"invalid log level: {level!r}")


class ContextLogger:
    """A stdlib logging facade enriched with Cordis context metadata."""

    def __init__(self, ctx: Context, name: str | None = None) -> None:
        self._ctx = ctx
        metadata = ctx.metadata("logger")
        config = metadata if isinstance(metadata, Mapping) else {}
        self.name = name or str(config.get("name") or ctx._fiber.name)
        self.level = _normalize_level(config.get("level"))
        self.extra = {
            "cordis_fiber": ctx._fiber.name,
            "cordis_fiber_uid": ctx._fiber.uid,
            **dict(config.get("extra", {})),
        }

    def log(self, level: int, message: object, *args: object, **kwargs: Any) -> None:
        if self.level is not None and level < self.level:
            return
        extra = {**self.extra, **dict(kwargs.pop("extra", {}))}
        logging.getLogger(self.name).log(level, message, *args, extra=extra, **kwargs)

    def debug(self, message: object, *args: object, **kwargs: Any) -> None:
        self.log(logging.DEBUG, message, *args, **kwargs)

    def info(self, message: object, *args: object, **kwargs: Any) -> None:
        self.log(logging.INFO, message, *args, **kwargs)

    def warning(self, message: object, *args: object, **kwargs: Any) -> None:
        self.log(logging.WARNING, message, *args, **kwargs)

    warn = warning

    def error(self, message: object, *args: object, **kwargs: Any) -> None:
        self.log(logging.ERROR, message, *args, **kwargs)

    def exception(self, message: object, *args: object, **kwargs: Any) -> None:
        kwargs.setdefault("exc_info", True)
        self.error(message, *args, **kwargs)

    def critical(self, message: object, *args: object, **kwargs: Any) -> None:
        self.log(logging.CRITICAL, message, *args, **kwargs)

    def add_handler(self, handler: logging.Handler) -> EffectHandle:
        """Attach a handler for exactly the lifetime of this Context scope."""
        logger = logging.getLogger(self.name)
        logger.addHandler(handler)

        def remove() -> None:
            logger.removeHandler(handler)
            handler.close()

        return self._ctx.defer(remove, label=f"logger:{self.name}")


class LoggerService:
    def bind(self, ctx: Context, name: str | None = None) -> ContextLogger:
        return ContextLogger(ctx, name)

    def __call__(self, ctx: Context, name: str | None = None) -> ContextLogger:
        return self.bind(ctx, name)


def console_handler(
    *,
    level: int = logging.INFO,
    format: str = "%(asctime)s %(levelname)s %(name)s: %(message)s",
) -> logging.Handler:
    handler = logging.StreamHandler()
    handler.setLevel(level)
    handler.setFormatter(logging.Formatter(format))
    return handler
