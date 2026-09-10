from __future__ import annotations

import math
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path


def parse_cents(value: str) -> int:
    """Parse positive decimal money without floating-point rounding."""
    value = value.strip()
    if not re.fullmatch(r"[0-9]+(?:\.[0-9]{1,2})?", value):
        raise ValueError("金额必须是最多两位小数的正数")
    whole, _, fraction = value.partition(".")
    cents = int(whole) * 100 + int(fraction.ljust(2, "0"))
    if not 0 < cents <= 2**63 - 1:
        raise ValueError("金额必须大于零且在 SQLite 整数范围内")
    return cents


def format_cents(value: int) -> str:
    return f"{value // 100}.{value % 100:02d}"


@dataclass(frozen=True)
class DatabaseConfig:
    path: Path


@dataclass(frozen=True)
class ValidationConfig:
    max_amount_cents: int


@dataclass(frozen=True)
class ImporterConfig:
    directory: Path
    interval: float


@dataclass(frozen=True)
class AppConfig:
    database: DatabaseConfig
    validation: ValidationConfig
    importer: ImporterConfig
    logging: bool


def _table(value: object, name: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError(f"{name} 必须是 TOML 表")
    result: dict[str, object] = {}
    for key, item in value.items():
        if not isinstance(key, str):
            raise ValueError(f"{name} 的键必须是字符串")
        result[key] = item
    return result


def _string(table: dict[str, object], key: str) -> str:
    value = table.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} 必须是非空字符串")
    return value


def parse_config(content: bytes, base: Path) -> AppConfig:
    """Validate the complete file before mutating any running plugins."""
    raw = _table(tomllib.loads(content.decode("utf-8")), "配置")
    database = _table(raw.get("database"), "database")
    validation = _table(raw.get("validation"), "validation")
    importer = _table(raw.get("importer"), "importer")
    logging = _table(raw.get("logging"), "logging")
    interval = importer.get("interval", 0.5)
    if (
        isinstance(interval, bool)
        or not isinstance(interval, (int, float))
        or not math.isfinite(interval)
        or interval < 0.05
    ):
        raise ValueError("importer.interval 必须是至少 0.05 秒的有限数字")
    enabled = logging.get("enabled", True)
    if not isinstance(enabled, bool):
        raise ValueError("logging.enabled 必须是布尔值")
    return AppConfig(
        database=DatabaseConfig((base / _string(database, "path")).resolve()),
        validation=ValidationConfig(parse_cents(_string(validation, "max_amount"))),
        importer=ImporterConfig(
            (base / _string(importer, "directory")).resolve(),
            float(interval),
        ),
        logging=enabled,
    )
