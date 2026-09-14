"""Compatibility runner for the audit cases, now ordinary passing regressions.

Run from python/: PYTHONPATH=src .venv/bin/python docs/repros/bug_audit_20260910.py -v
The former --strict flag is accepted; no test is marked expectedFailure.
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tests.test_regressions import CoreAudit, HMRAudit, LoaderAudit  # noqa: E402,F401


if __name__ == "__main__":
    if "--strict" in sys.argv:
        sys.argv.remove("--strict")
    unittest.main()
