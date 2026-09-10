import asyncio
import sqlite3
import tempfile
import unittest
from pathlib import Path

from examples.order_importer.application import Application
from examples.order_importer.config import ValidationConfig, parse_config
from examples.order_importer.contracts import AppContext, ImportReport
from examples.order_importer.plugins import SQLiteStore


CONFIG = """[database]
path = "var/orders.sqlite3"
[validation]
max_amount = "50.00"
[importer]
directory = "incoming"
interval = 0.05
[logging]
enabled = false
"""
CSV = "order_id,customer,amount\nA,Alice,25.00\nB,Bob,80.00\nC,Carol,-5.00\n"


class OrderImporterTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.base = Path(directory.name)
        self.path = self.base / "config.toml"
        self.path.write_text(CONFIG, encoding="utf-8")
        self.incoming = self.base / "incoming"
        self.incoming.mkdir()

    async def test_file_watch_reloads_policy_and_recovers_after_invalid_config(self):
        (self.incoming / "orders.csv").write_text(CSV, encoding="utf-8")
        async with AppContext() as root:
            reports = asyncio.Queue()
            errors = asyncio.Queue()
            root.on("orders/imported", reports.put_nowait)
            root.on("config/error", errors.put_nowait)
            app = Application(root, self.path)
            await app.refresh()
            root.spawn(app.watch(0.01), policy="cancel")
            first = await asyncio.wait_for(reports.get(), 3)
            self.assertEqual((first.accepted, len(first.rejected)), (1, 2))
            old_validator = root.validator
            old_importer = root.importer
            old_store = root.store

            for invalid in ("[validation", CONFIG.replace('"50.00"', '"NaN"')):
                self.path.write_text(invalid, encoding="utf-8")
                await asyncio.wait_for(errors.get(), 3)
                self.assertIs(root.validator, old_validator)
                self.assertIs(root.importer, old_importer)
                self.assertIs(root.store, old_store)

            self.path.write_text(
                CONFIG.replace('"50.00"', '"100.00"'), encoding="utf-8"
            )
            second = await asyncio.wait_for(reports.get(), 3)
            self.assertEqual((second.accepted, len(second.rejected)), (2, 1))
            self.assertIsNot(root.validator, old_validator)
            self.assertIsNot(root.importer, old_importer)
            self.assertIs(root.store, old_store)
            self.assertEqual([order.order_id for order in root.store.all()], ["A", "B"])

            # CSV changes are detected even when the TOML remains unchanged.
            (self.incoming / "orders.csv").write_text(
                "order_id,customer,amount\nA,Alice,30.00\nD,Dora,99.99\n",
                encoding="utf-8",
            )
            third = await asyncio.wait_for(reports.get(), 3)
            self.assertEqual(third.accepted, 2)
            self.assertEqual(
                [(order.order_id, order.amount_cents) for order in root.store.all()],
                [("A", 3000), ("B", 8000), ("D", 9999)],
            )

    async def test_running_import_drains_with_old_policy_and_database(self):
        manual = self.base / "manual.csv"
        manual.write_text(
            "order_id,customer,amount\n"
            + "".join(f"{index},Customer,80.00\n" for index in range(100)),
            encoding="utf-8",
        )
        async with AppContext() as root:
            app = Application(root, self.path)
            await app.refresh()
            task = root.importer.submit(manual)
            await asyncio.sleep(0)
            self.assertFalse(task.done())
            validator = app.loader.get("validation")
            self.assertIsNotNone(validator)
            await validator.update(ValidationConfig(10000))
            await root.flush()
            first = await task
            self.assertIsInstance(first, ImportReport)
            self.assertEqual((first.accepted, len(first.rejected)), (0, 100))

            old_store = root.store
            self.assertIsInstance(old_store, SQLiteStore)
            task = root.importer.submit(manual)
            await asyncio.sleep(0)
            self.assertFalse(task.done())
            database = app.loader.get("database")
            self.assertIsNotNone(database)
            await database.restart()
            await root.flush()
            second = await task
            self.assertEqual(second.accepted, 100)
            self.assertEqual(len(root.store.all()), 100)
            with self.assertRaises(sqlite3.ProgrammingError):
                old_store.all()
            self.assertEqual(root.errors, ())

    async def test_logging_toggle_preserves_importer_and_removes_listener(self):
        async with AppContext() as root:
            app = Application(root, self.path)
            await app.refresh()
            service = root.importer
            self.assertIsNone(app.loader.get("console"))
            self.path.write_text(CONFIG.replace("enabled = false", "enabled = true"))
            await app.refresh()
            self.assertIs(root.importer, service)
            self.assertIsNotNone(app.loader.get("console"))
            self.path.write_text(CONFIG)
            await app.refresh()
            self.assertIs(root.importer, service)
            self.assertIsNone(app.loader.get("console"))

    def test_config_rejects_invalid_amounts_and_poll_intervals(self):
        for amount in ("NaN", "Infinity", "-1", "0.00", "0.001"):
            with self.subTest(amount=amount), self.assertRaises(ValueError):
                parse_config(
                    CONFIG.replace('"50.00"', f'"{amount}"').encode(), self.base
                )
        for interval in ("true", "0", "-1", "nan", "inf"):
            with self.subTest(interval=interval), self.assertRaises(ValueError):
                parse_config(
                    CONFIG.replace(
                        "interval = 0.05", f"interval = {interval}"
                    ).encode(),
                    self.base,
                )


if __name__ == "__main__":
    unittest.main()
