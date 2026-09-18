import asyncio
import importlib
import logging
import sys
import tempfile
import unittest
from pathlib import Path

from cordis import (
    Component,
    ConfigIncludes,
    ConfigStore,
    Context,
    Entry,
    EntryTree,
    EventKey,
    FiberState,
    Loader,
    Service,
    ServiceKey,
    StaleConfigError,
    decode_schema,
    import_plugin,
    plugin,
)
from cordis.hmr import ModuleGraph
from tests import sample_plugins


class _Worker(Service[None]):
    def install(self, seen):
        return self.ctx.on("work", seen.append)


WORKER = ServiceKey[_Worker]("worker")


class EnhancementTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        sample_plugins.events.clear()

    async def test_service_effects_belong_to_calling_context(self):
        seen = []

        @plugin(provide=[WORKER])
        def provider(ctx, _config):
            _Worker(ctx, WORKER)

        @plugin(inject=[WORKER])
        def consumer(ctx, _config):
            ctx.service(WORKER).install(seen)

        root = Context()
        provider_fiber = await root.use(provider)
        consumer_fiber = await root.use(consumer)
        root.emit("work", 1)
        await consumer_fiber.dispose()
        root.emit("work", 2)

        self.assertEqual(seen, [1])
        self.assertEqual(provider_fiber.state, FiberState.ACTIVE)

    async def test_accessor_mixin_is_writable_and_lifecycle_bound(self):
        class Profile:
            nickname = "cordis"

        @plugin(provide=["profile"])
        def provider(ctx, _config):
            ctx.provide("profile", Profile())
            ctx.mixin("profile", ["nickname"])

        root = Context()
        fiber = await root.use(provider)
        self.assertEqual(root.nickname, "cordis")
        root.nickname = "python"
        self.assertEqual(root.profile.nickname, "python")
        await fiber.dispose()
        with self.assertRaises(Exception):
            _ = root.nickname

    async def test_registry_restarts_and_disposes_selected_components(self):
        starts = 0
        stops = 0

        @plugin
        def sample(_ctx, _config):
            nonlocal starts, stops
            starts += 1

            def stop():
                nonlocal stops
                stops += 1

            return stop

        root = Context()
        fiber = await root.use(sample)
        self.assertEqual(root.registry.find(sample), (fiber,))
        await root.registry.restart(sample)
        self.assertEqual((starts, stops), (2, 1))
        await root.registry.dispose(sample)
        self.assertEqual((starts, stops), (2, 2))

    async def test_variadic_event_key_dispatches_multiple_arguments(self):
        progress = EventKey[int, str]("progress")
        seen = []
        root = Context()
        root.on(progress, lambda count, label: seen.append((count, label)))
        root.emit(progress, 2, "ready")
        await root.parallel(progress, 3, "done")
        self.assertEqual(seen, [(2, "ready"), (3, "done")])

    async def test_schema_adapter_supports_common_validation_protocols(self):
        class Adapter:
            def validate_python(self, value):
                return int(value)

        self.assertEqual(await decode_schema(Adapter(), "42"), 42)

        component = Component(lambda _ctx, config: None)
        binding = await component.decode_schema("7", Adapter())
        self.assertEqual(binding.config, 7)

    async def test_logger_handler_and_timer_follow_component_lifetime(self):
        records = []
        handlers = []

        class Handler(logging.Handler):
            def emit(self, record):
                records.append(record.getMessage())

        tasks = []

        @plugin
        def sample(ctx, _config):
            handler = Handler()
            handlers.append(handler)
            ctx.logger.add_handler(handler)
            ctx.logger.warning("started")
            tasks.append(ctx.timer.timeout(60, lambda: None))

        root = Context()
        fiber = await root.use(sample)
        logger_name = fiber.ctx.logger.name
        await fiber.dispose()

        self.assertEqual(records, ["started"])
        self.assertNotIn(handlers[0], logging.getLogger(logger_name).handlers)
        self.assertTrue(tasks[0].cancelled())

    async def test_include_applies_scope_without_changing_context_type(self):
        class AppContext(Context):
            pass

        contexts = []

        @plugin
        def sample(ctx, _config):
            contexts.append(ctx)

        root = AppContext()
        await root.include(
            sample,
            isolate=("value",),
            intercept={"value": {"source": "include"}},
        )
        self.assertIsInstance(contexts[0], AppContext)
        self.assertEqual(contexts[0].metadata("value"), {"source": "include"})

    async def test_entry_tree_and_config_store_are_transactional(self):
        group = Entry("group")
        tree = EntryTree((group,)).create(
            Entry("sample", "tests.sample_plugins:configured", {"value": 1}),
            parent="group",
        )
        self.assertEqual(tree.get("group/sample").config, {"value": 1})
        moved = tree.move("group/sample")
        self.assertEqual(moved.get("sample").id, "sample")
        self.assertEqual(tree.get("group/sample").id, "sample")

        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary, "cordis.json")
            path.write_text("[]\n", encoding="utf-8")
            root = Context()
            loader = Loader(root)
            store = ConfigStore.file(loader, path)
            await store.load()
            await store.create(
                Entry("sample", "tests.sample_plugins:configured", {"value": 1})
            )
            await store.update("sample", config={"value": 2})
            self.assertEqual(loader.entries[0].config, {"value": 2})
            await asyncio.gather(
                store.create(Entry("left", "tests.sample_plugins:configured", "left")),
                store.create(
                    Entry("right", "tests.sample_plugins:configured", "right")
                ),
            )
            self.assertEqual(
                {entry.id for entry in store.tree.entries},
                {"sample", "left", "right"},
            )

            path.write_text("[]\n", encoding="utf-8")
            with self.assertRaises(StaleConfigError):
                await store.update("sample", config={"value": 3})
            self.assertEqual(loader.entries[0].config, {"value": 2})

    async def test_empty_group_is_a_valid_loader_entry(self):
        root = Context()
        loader = Loader(root)
        await loader.reconcile([Entry("group")])
        self.assertEqual(loader.get("group").state, FiberState.ACTIVE)

    async def test_include_files_are_independent_nested_stores(self):
        with tempfile.TemporaryDirectory() as temporary:
            left = Path(temporary, "left.json")
            right = Path(temporary, "right.json")
            left.write_text(
                '[{"id":"worker","plugin":"tests.sample_plugins:configured",'
                '"config":"left"}]\n',
                encoding="utf-8",
            )
            right.write_text(
                '[{"id":"worker","plugin":"tests.sample_plugins:configured",'
                '"config":"right"}]\n',
                encoding="utf-8",
            )
            root = Context()
            loader = Loader(root)
            includes = ConfigIncludes(loader)
            left_store = await includes.include_file("left", left)
            await includes.include_file("right", right)

            self.assertEqual(set(includes.names), {"left", "right"})
            self.assertIsNotNone(loader.child("left"))
            await left_store.update("worker", config="updated")
            self.assertIn("updated", left.read_text(encoding="utf-8"))

            await includes.remove("right")
            self.assertEqual(includes.names, ("left",))


class RuntimeImportTracingTests(unittest.TestCase):
    def test_dynamic_import_is_added_to_module_graph(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            package = root / "traced_package"
            package.mkdir()
            (package / "__init__.py").write_text("", encoding="utf-8")
            (package / "helper.py").write_text("VALUE = 1\n", encoding="utf-8")
            (package / "plugin.py").write_text(
                "import importlib\n"
                "helper = importlib.import_module('traced_package.helper')\n"
                "def sample(ctx, config):\n"
                "    return None\n",
                encoding="utf-8",
            )
            sys.path.insert(0, temporary)
            importlib.invalidate_caches()
            try:
                import_plugin("traced_package.plugin:sample")
                graph = ModuleGraph()
                self.assertIn(
                    "traced_package.helper", graph.imports["traced_package.plugin"]
                )
            finally:
                sys.path.remove(temporary)
                for name in tuple(sys.modules):
                    if name == "traced_package" or name.startswith("traced_package."):
                        sys.modules.pop(name, None)


if __name__ == "__main__":
    unittest.main()
