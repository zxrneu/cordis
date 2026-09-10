import asyncio
import ast
import importlib.util
import tempfile
import unittest
from pathlib import Path

from examples.order_importer.application import Application
from examples.order_importer.config import parse_config
from examples.order_importer.definitions import (
    AppContext,
    ImportReport,
    ImportRequest,
    Order,
    StoreClosedError,
)
from examples.order_importer.providers.csv import importer
from examples.order_importer.providers.sqlite import DatabaseConfig, database
from examples.order_importer.providers.validation import ValidationConfig, validation


CONFIG = """[database]
provider = "sqlite"
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
            task = root.importer.submit(ImportRequest(manual))
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
            task = root.importer.submit(ImportRequest(manual))
            await asyncio.sleep(0)
            self.assertFalse(task.done())
            database = app.loader.get("database")
            self.assertIsNotNone(database)
            await database.restart()
            await root.flush()
            second = await task
            self.assertEqual(second.accepted, 100)
            self.assertEqual(len(root.store.all()), 100)
            with self.assertRaises(StoreClosedError):
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

    async def test_storage_providers_obey_the_same_behavior_contract(self):
        for provider in ("sqlite", "memory"):
            with self.subTest(provider=provider):
                self.path.write_text(
                    CONFIG.replace('provider = "sqlite"', f'provider = "{provider}"')
                )
                async with AppContext() as root:
                    app = Application(root, self.path)
                    await app.refresh()
                    store = root.store
                    self.assertEqual(store.all(), ())
                    store.save(Order("B", "Bob", 100))
                    store.save(Order("A", "Alice", 200))
                    snapshot = store.all()
                    self.assertEqual(
                        snapshot, (Order("A", "Alice", 200), Order("B", "Bob", 100))
                    )
                    store.save(Order("A", "Alice updated", 300))
                    self.assertEqual(
                        store.all(),
                        (Order("A", "Alice updated", 300), Order("B", "Bob", 100)),
                    )
                    self.assertEqual(snapshot[0], Order("A", "Alice", 200))
                    store_fiber = app.loader.get("database")
                    await store_fiber.dispose()
                    with self.assertRaises(StoreClosedError):
                        store.all()
                    with self.assertRaises(StoreClosedError):
                        store.save(Order("C", "Carol", 100))

    async def test_file_watcher_switches_providers_without_changing_consumers(self):
        (self.incoming / "orders.csv").write_text(CSV, encoding="utf-8")
        async with AppContext() as root:
            reports = asyncio.Queue()
            errors = asyncio.Queue()
            root.on("orders/imported", reports.put_nowait)
            root.on("config/error", errors.put_nowait)
            app = Application(root, self.path)
            await app.refresh()
            root.spawn(app.watch(0.01), policy="cancel")
            await asyncio.wait_for(reports.get(), 3)
            first_store = root.store
            validator_service = root.validator
            consumer = app.loader.get("directory").component

            self.path.write_text(
                CONFIG.replace('provider = "sqlite"', 'provider = "unknown"')
            )
            await asyncio.wait_for(errors.get(), 3)
            self.assertIs(root.store, first_store)

            self.path.write_text(
                CONFIG.replace('provider = "sqlite"', 'provider = "memory"')
            )
            report = await asyncio.wait_for(reports.get(), 3)
            self.assertEqual((report.accepted, len(report.rejected)), (1, 2))
            self.assertIsNot(root.store, first_store)
            self.assertIs(root.validator, validator_service)
            self.assertIs(app.loader.get("directory").component, consumer)
            self.assertEqual(root.store.all(), (Order("A", "Alice", 2500),))
            with self.assertRaises(StoreClosedError):
                first_store.all()

            root.store.save(Order("memory-only", "Temporary", 100))
            self.path.write_text(CONFIG)
            report = await asyncio.wait_for(reports.get(), 3)
            self.assertEqual(report.accepted, 1)
            self.assertEqual(root.store.all(), (Order("A", "Alice", 2500),))
            self.assertIs(app.loader.get("directory").component, consumer)

    async def test_interval_change_only_restarts_directory_consumer(self):
        async with AppContext() as root:
            app = Application(root, self.path)
            await app.refresh()
            service = root.importer
            old_task = app.loader.get("directory").tasks[0]
            self.path.write_text(CONFIG.replace("interval = 0.05", "interval = 0.1"))
            await app.refresh()
            self.assertIs(root.importer, service)
            self.assertTrue(old_task.done())
            self.assertIsNot(app.loader.get("directory").tasks[0], old_task)

    async def test_import_provider_returns_data_without_publishing_ui_events(self):
        manual = self.base / "manual.csv"
        manual.write_text(CSV, encoding="utf-8")
        async with AppContext() as root:
            reports = []
            root.on("orders/imported", reports.append)
            await root.use(database, DatabaseConfig(self.base / "manual.sqlite3"))
            await root.use(validation, ValidationConfig(5000))
            await root.use(importer)
            result = await root.importer.submit(ImportRequest(manual))
            self.assertEqual((result.accepted, len(result.rejected)), (1, 2))
            self.assertEqual(reports, [])

    def test_roles_do_not_import_each_others_implementations(self):
        package = Path(__file__).resolve().parents[1] / "examples/order_importer"
        prefix = "examples.order_importer"
        graph = {}
        for path in package.rglob("*.py"):
            relative = path.relative_to(package).with_suffix("")
            parts = list(relative.parts)
            if parts[-1] == "__init__":
                parts.pop()
            module = ".".join((prefix, *parts))
            parent = module if path.name == "__init__.py" else module.rpartition(".")[0]
            dependencies = set()
            for node in ast.walk(ast.parse(path.read_text())):
                if isinstance(node, ast.ImportFrom):
                    target = "." * node.level + (node.module or "")
                    target = (
                        importlib.util.resolve_name(target, parent)
                        if node.level
                        else target
                    )
                    dependencies.add(target)
                    # Include `from . import providers` and similar module imports.
                    dependencies.update(
                        f"{target}.{alias.name}"
                        for alias in node.names
                        if alias.name != "*"
                    )
                elif isinstance(node, ast.Import):
                    dependencies.update(alias.name for alias in node.names)
            graph[module] = dependencies
        for module in graph:
            role = module.removeprefix(prefix + ".").partition(".")[0]
            if role not in ("definitions", "providers", "consumers"):
                continue
            forbidden = {"config", "application", "plugins", "__main__"}
            forbidden.add("consumers" if role == "providers" else "providers")
            if role == "definitions":
                forbidden.add("consumers")
            seen = set()
            pending = list(graph[module])
            while pending:
                dependency = pending.pop()
                if dependency in seen or not dependency.startswith(prefix + "."):
                    continue
                seen.add(dependency)
                self.assertNotIn(
                    dependency.removeprefix(prefix + ".").partition(".")[0],
                    forbidden,
                    f"{module} depends on {dependency}",
                )
                pending.extend(graph.get(dependency, ()))


if __name__ == "__main__":
    unittest.main()
