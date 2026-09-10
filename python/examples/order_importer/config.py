from __future__ import annotations

import math
import tomllib
from dataclasses import dataclass
from pathlib import Path

from .consumers.directory import ImporterConfig as ImporterConfig
from .money import format_cents as format_cents, parse_cents
from .providers.memory import MemoryConfig
from .providers.sqlite import DatabaseConfig as DatabaseConfig
from .providers.validation import ValidationConfig as ValidationConfig


@dataclass(frozen=True)
class AppConfig:
    database: DatabaseConfig | MemoryConfig
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
    provider = database.get("provider", "sqlite")
    store_config: DatabaseConfig | MemoryConfig
    if provider == "sqlite":
        store_config = DatabaseConfig((base / _string(database, "path")).resolve())
    elif provider == "memory":
        store_config = MemoryConfig()
    else:
        raise ValueError("database.provider 必须是 sqlite 或 memory")
    return AppConfig(
        database=store_config,
        validation=ValidationConfig(parse_cents(_string(validation, "max_amount"))),
        importer=ImporterConfig(
            (base / _string(importer, "directory")).resolve(),
            float(interval),
        ),
        logging=enabled,
    )
