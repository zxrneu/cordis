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

    async def test_throttled_coalesces_burst_and_supports_cancel(self):
        seen = []
        root = Context()
        throttled = root.timer.throttle(0.05, seen.append)

        # High-frequency burst: call 1 runs immediately, 2 and 3 coalesce to trailing (3)
        throttled(1)
        throttled(2)
        throttled(3)
        await asyncio.sleep(0)
        self.assertEqual(seen, [1])

        await asyncio.sleep(0.07)
        self.assertEqual(seen, [1, 3])

        # Wait for cooldown so 4 becomes a new leading call
        await asyncio.sleep(0.06)
        throttled(4)
        await asyncio.sleep(0)
        self.assertEqual(seen, [1, 3, 4])

        throttled(5)
        throttled.cancel()
        await asyncio.sleep(0.07)
        self.assertEqual(seen, [1, 3, 4])

    async def test_logger_string_level_normalization(self):
        root = Context()
        ctx_info = root.intercept("logger", {"level": "info"})
        self.assertEqual(ctx_info.logger.level, logging.INFO)

        ctx_debug = root.intercept("logger", {"level": "DEBUG"})
        self.assertEqual(ctx_debug.logger.level, logging.DEBUG)

        ctx_warn = root.intercept("logger", {"level": "WARNING"})
        self.assertEqual(ctx_warn.logger.level, logging.WARNING)

    async def test_config_store_watch_survives_syntax_error(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary, "cordis.json")
            path.write_text("[]\n", encoding="utf-8")
            root = Context()
            loader = Loader(root)
            store = ConfigStore.file(loader, path)
            await store.load()

            errors = []
            stop = asyncio.Event()
            watch_task = asyncio.create_task(
                store.watch(interval=0.01, stop=stop, on_error=errors.append)
            )

            # Write broken JSON: watch shouldn't crash
            path.write_text("broken json [", encoding="utf-8")
            await asyncio.sleep(0.03)
            self.assertGreaterEqual(len(errors), 1)
            self.assertFalse(watch_task.done())

            # Write valid JSON: watch recovers on next read
            path.write_text(
                '[{"id":"sample","plugin":"tests.sample_plugins:configured","config":"recovered"}]\n',
                encoding="utf-8",
            )
            await asyncio.sleep(0.03)
            self.assertEqual(loader.entries[0].config, "recovered")

            stop.set()
            await watch_task

    async def test_bound_service_fluent_method_chaining(self):
        class Builder(Service[None]):
            def __init__(self, ctx, key):
                super().__init__(ctx, key)
                self.actions = []

            def step_one(self, name):
                self.actions.append((name, self.ctx))
                return self

            def step_two(self, name):
                self.actions.append((name, self.ctx))
                return self

        BUILDER = ServiceKey[Builder]("builder")

        @plugin(provide=[BUILDER])
        def provider(ctx, _config):
            Builder(ctx, BUILDER)

        consumed_contexts = []

        @plugin(inject=[BUILDER])
        def consumer(ctx, _config):
            consumed_contexts.append(ctx)
            ctx.service(BUILDER).step_one("first").step_two("second")

        root = Context()
        await root.use(provider)
        await root.use(consumer)

        b = root.service(BUILDER)
        self.assertEqual(len(b.actions), 2)
        self.assertEqual(b.actions[0][0], "first")
        self.assertEqual(b.actions[0][1], consumed_contexts[0])
        self.assertEqual(b.actions[1][0], "second")
        self.assertEqual(b.actions[1][1], consumed_contexts[0])

    async def test_serial_and_bail_with_event_key(self):
        EVENT = EventKey[int, str]("event")
        root = Context()
        root.on(EVENT, lambda code, msg: f"handled:{code}:{msg}" if code > 0 else None)

        bail_res = root.bail(EVENT, 1, "bail_test")
        self.assertEqual(bail_res, "handled:1:bail_test")

        serial_res = await root.serial(EVENT, 2, "serial_test")
        self.assertEqual(serial_res, "handled:2:serial_test")


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
