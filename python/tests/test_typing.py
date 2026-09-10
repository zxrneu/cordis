import importlib.util
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


@unittest.skipUnless(
    importlib.util.find_spec("mypy"), "install dev dependencies to run type tests"
)
class StaticTypingTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(__file__).resolve().parents[1]
        self.cache = tempfile.TemporaryDirectory()
        self.addCleanup(self.cache.cleanup)

    def check(self, *paths):
        return subprocess.run(
            [
                sys.executable,
                "-m",
                "mypy",
                "--config-file",
                "pyproject.toml",
                "--cache-dir",
                self.cache.name,
                "--no-pretty",
                "--hide-error-context",
                "--no-error-summary",
                *paths,
            ],
            cwd=self.root,
            text=True,
            capture_output=True,
            timeout=60,
        )

    def test_public_api_and_inferred_types_pass_strict_checking(self):
        result = self.check("src", "examples/demo.py", "typing_tests/contracts.py")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_invalid_calls_are_rejected_at_every_marked_line(self):
        fixture = self.root / "typing_tests/invalid.py"
        expected = {
            number
            for number, line in enumerate(fixture.read_text().splitlines(), 1)
            if "# E" in line
        }
        result = self.check("typing_tests/invalid.py")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        diagnostics = re.findall(r"^(.+?):(\d+): error:", result.stdout, re.MULTILINE)
        self.assertTrue(diagnostics, result.stdout + result.stderr)
        self.assertEqual(
            {Path(path).name for path, _ in diagnostics}, {"invalid.py"}, result.stdout
        )
        self.assertEqual(
            {int(line) for _, line in diagnostics}, expected, result.stdout
        )
