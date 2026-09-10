"""Compatibility exports; composition lives in application.py.

The importer plugin now provides one-file import only. Mount directory separately
when directory scanning is needed.
"""

from .consumers.console import console as console
from .consumers.directory import directory as directory
from .providers.csv import CsvImporter as CsvImporter, importer as importer
from .providers.memory import MemoryStore as MemoryStore, memory as memory
from .providers.sqlite import SQLiteStore as SQLiteStore, database as database
from .providers.validation import (
    AmountValidator as AmountValidator,
    validation as validation,
)
