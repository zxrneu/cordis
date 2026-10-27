import asyncio
import gc
import importlib
import json
import sys
import tempfile
import unittest
import weakref
from pathlib import Path
from uuid import uuid4

from cordis import (
    ConfigExpression,
    ConfigPatch,
    ConfigStore,
    Context,
    ContextAware,
    DuplicateServiceError,
    Entry,
    FiberState,
    InactiveContextError,
    Loader,
    Service,
    UndeclaredServiceError,
    current_context,
    import_plugin,
    plugin,
)
from cordis.config import EntryTree, JsonCodec
from cordis.patches import merge_trees


class MemoryBackend:
    identity = "memory"

    def __init__(self, *, fail=False):
        self.fail = fail
        self.text = (
            '[{"id":"worker","plugin":"tests.sample_plugins:configured","config":1}]'
        )

    async def read(self):
        return self.text

    async def write(self, text):
        if self.fail:
            raise OSError("read only")
        self.text = text


class CoreParityTests(unittest.IsolatedAsyncioTestCase):
    async def test_awaitable_service_values_are_not_middleware(self):
        async with Context() as root:
            future = asyncio.get_running_loop().create_future()
            root.provide("future", future)
            root.on("internal/get", lambda ctx, name, next_step: next_step())
            self.assertIs(root.service("future"), future)
            future.set_result(42)
            self.assertEqual(await root.service("future"), 42)

    async def test_service_getter_rejects_async_middleware(self):
        async with Context() as root:
            root.provide("value", 1)

            async def middleware(ctx, name, next_step):
                return next_step()

            root.on("internal/get", middleware)
            with self.assertRaisesRegex(TypeError, "synchronous"):
                root.service("value")

    async def test_service_dependencies_and_effect_ownership(self):
        seen, values = [], []

        class Api(Service):
            def read(self):
                self.ctx.on("used", seen.append)
                return self.ctx.db

        async with Context() as root:
            root.provide("db", 42)
            provider = await root.inject(["db"], lambda ctx, _: Api(ctx, "api"))
            consumer = await root.inject(
                ["api"], lambda ctx, _: values.append(ctx.api.read())
            )
            root.emit("used", 1)
            await consumer.dispose()
            root.emit("used", 2)
            self.assertEqual(
                (values, seen, provider.state), ([42], [1], FiberState.ACTIVE)
            )

    async def test_root_does_not_bypass_service_inject(self):
        class Probe(Service):
            def read(self):
                return self.ctx.secret

        async with Context() as root:
            await root.use(lambda ctx, _: ctx.provide("secret", "hidden"))
            await root.use(lambda ctx, _: Probe(ctx, "probe"))
            with self.assertRaises(UndeclaredServiceError):
                root.probe.read()

    async def test_service_keeps_provider_realm_through_derived_views(self):
        class Probe(Service):
            def read(self):
                return self.ctx.intercept("db", {"trace": True}).db

        async with Context() as root:
            root.provide("db", "public")
            private = root.isolate("db")
            await private.use(lambda ctx, _: ctx.provide("db", "private"))
            await private.inject(["db"], lambda ctx, _: Probe(ctx, "probe"))
            self.assertEqual(root.probe.read(), "private")

    async def test_derived_service_view_cannot_bypass_inject(self):
        class Probe(Service):
            def read(self):
                return self.ctx.extend(trace=True).secret

        async with Context() as root:
            await root.use(lambda ctx, _: ctx.provide("secret", "hidden"))
            await root.use(lambda ctx, _: Probe(ctx, "probe"))
            with self.assertRaises(UndeclaredServiceError):
                root.probe.read()

    async def test_self_service_reads_and_owned_assignment(self):
        seen = []

        def owner(ctx, _):
            ctx.provide("own", 1)
            seen.append(ctx.own)
            ctx.own = 2
            return lambda: seen.append(ctx.own)

        async with Context() as root:
            fiber = await root.use(owner)
            consumer = await root.inject(["own"], lambda ctx, _: None)
            with self.assertRaises(DuplicateServiceError):
                consumer.ctx.own = 3
            with self.assertRaises(UndeclaredServiceError):
                consumer.ctx.unprovided = 3
            self.assertEqual(consumer.ctx.own, 2)
            await fiber.dispose()
            self.assertEqual(seen, [1, 2])

    async def test_metadata_and_subclass_state_inherit(self):
        class App(Context):
            def __init__(self):
                super().__init__()
                self._state = []

        async with App() as root:
            derived = root.extend(port=8000)
            fiber = await derived.use(lambda ctx, _: ctx._state.append(ctx.port))
            derived.port = 9000
            self.assertIs(fiber.ctx._state, root._state)
            self.assertEqual((root._state, fiber.ctx.port), ([8000], 9000))

    async def test_context_aware_can_opt_out_of_definition_scope(self):
        class Probe(ContextAware):
            def read(self):
                return self.ctx.db

        async with Context() as root:
            root.provide("db", 7)
            await root.use(
                lambda ctx, _: ctx.provide(
                    "probe", Probe(ctx, preserve_dependencies=False)
                )
            )
            result = []
            await root.inject(
                ["probe", "db"], lambda ctx, _: result.append(ctx.probe.read())
            )
            self.assertEqual(result, [7])

    async def test_association_uses_accessing_contract(self):
        async with Context() as root:
            await root.use(lambda ctx, _: Service(ctx, "foo"))
            await root.use(lambda ctx, _: Service(ctx, "foo.bar"))
            allowed = await root.inject(["foo", "foo.bar"], lambda ctx, _: None)
            denied = await root.inject(["foo"], lambda ctx, _: None)
            self.assertEqual(allowed.ctx.foo.bar.service_key, "foo.bar")
            with self.assertRaises(UndeclaredServiceError):
                _ = denied.ctx.foo.bar

    async def test_associated_property_receiver(self):
        class Session(ContextAware):
            secret = 1

        class Extension:
            @property
            def score(self):
                return self.secret

            @score.setter
            def score(self, value):
                self.secret = value + 1

        async with Context() as root:
            root.mixin(Extension(), ["score"], namespace="session")
            root.provide("session", Session(root, namespace="session"))
            session = root.session
            session.score = 10
            self.assertEqual((session.score, session.secret), (11, 11))

    async def test_service_check_and_config_merge(self):
        class Api(Service):
            available = False

            def check(self):
                return self.available and self.resolve_config().get("allow", False)

        async with Context() as root:
            instances = []

            def provide(ctx, _):
                instances.append(Api(ctx, "api"))

            await root.use(provide)
            ctx = root.intercept("api", {"allow": True, "values": [2]})
            fiber = ctx.inject(["api"], lambda ctx, _: None)
            self.assertEqual(fiber.state, FiberState.PENDING)
            instances[0].available = True
            root.notify("api")
            await root.flush()
            self.assertEqual(fiber.state, FiberState.ACTIVE)
            merged = ctx.api.resolve_config(
                {"values": [1]},
                {"values": [3]},
                merge=lambda *layers: [
                    x for layer in layers for x in layer.get("values", [])
                ],
            )
            self.assertEqual(merged, [1, 2, 3])

    async def test_iterators_do_not_leak_context(self):
        class Api(Service):
            def items(self):
                yield self.ctx.db

            async def async_items(self):
                yield self.ctx.db

        async with Context() as root:
            root.provide("db", 7)
            await root.inject(["db"], lambda ctx, _: Api(ctx, "api"))
            fiber = await root.inject(["api"], lambda ctx, _: None)
            iterator = fiber.ctx.api.items()
            self.assertEqual(next(iterator), 7)
            self.assertIsNone(current_context())
            iterator.close()
            iterator = fiber.ctx.api.async_items()
            self.assertEqual(await anext(iterator), 7)
            self.assertIsNone(current_context())
            await iterator.aclose()
            self.assertIsNone(current_context())

    async def test_effect_tree_snapshot(self):
        async with Context() as root:

            async def outer():
                await root.effect(
                    lambda: root.defer(lambda: None, label="leaf"), "inner"
                )

            handle = await root.effect(outer, "outer")
            snapshot = root.fiber.effects
            self.assertEqual(snapshot[0].children[0].children[0].label, "leaf")
            await handle()
            self.assertEqual(root.fiber.effects, ())
            self.assertEqual(snapshot[0].label, "outer")


class EventParityTests(unittest.IsolatedAsyncioTestCase):
    async def test_once_releases_intercepted_registration(self):
        async with Context() as root:
            callbacks = []
            cleaned = asyncio.Event()
            seen = []

            def intercept(ctx, name, callback, options):
                if name != "external":
                    return None
                callbacks.append(callback)

                async def remove():
                    callbacks.clear()
                    cleaned.set()

                return remove

            root.on("internal/listener", intercept)
            handle = root.once("external", lambda: seen.append(1))
            callbacks[0]()
            self.assertEqual(seen, [1])
            await handle()
            await asyncio.wait_for(cleaned.wait(), 1)
            self.assertEqual(callbacks, [])
            self.assertNotIn("external", root._runtime.listeners)

    async def test_once_is_synchronous_reentrant_safe_and_works_with_bail(self):
        async with Context() as root:
            seen = []

            def once():
                seen.append(1)
                root.emit("event")
                return 0

            root.once("event", once)
            self.assertEqual(root.bail("event"), 0)
            self.assertEqual(seen, [1])
            self.assertIsNone(root.bail("event"))
            self.assertNotIn("event", root._runtime.listeners)

    async def test_async_once_runs_only_once(self):
        async with Context() as root:
            seen = []

            async def once():
                await asyncio.sleep(0)
                seen.append(1)

            root.once("event", once)
            await asyncio.gather(root.parallel("event"), root.parallel("event"))
            self.assertEqual(seen, [1])

    async def test_emit_propagates_synchronous_error(self):
        async with Context() as root:
            root.on("event", lambda: (_ for _ in ()).throw(ValueError("boom")))
            with self.assertRaisesRegex(ValueError, "boom"):
                root.emit("event")

    async def test_waterfall_duplicate_next_after_await(self):
        async with Context() as root:
            seen = []

            async def middleware(next_step):
                await next_step()
                await asyncio.sleep(0)
                return await next_step()

            async def final():
                seen.append(1)

            root.on("event", middleware)
            with self.assertRaisesRegex(RuntimeError, "multiple times"):
                await root.waterfall("event", final=final)
            self.assertEqual(seen, [1])

    async def test_internal_events_and_scoped_update_veto(self):
        async with Context() as root:
            seen = []
            for name in ("plugin", "service", "listener", "dispatch"):
                root.on("internal/" + name, lambda *args, name=name: seen.append(name))

            def owner(ctx, config):
                ctx.provide("value", config)
                ctx.on(
                    "internal/update", lambda fiber, config, no_save, next_step: None
                )

            first = await root.use(owner, 1)
            second = await root.use(lambda ctx, config: None, 1)
            await first.update(2)
            await second.update(2)
            root.emit("business", 1)
            self.assertEqual((first.config, second.config), (1, 2))
            self.assertTrue({"plugin", "service", "listener", "dispatch"} <= set(seen))

    async def test_get_set_and_listener_interception(self):
        async with Context() as root:
            root.provide("value", 1)
            root.on(
                "internal/get",
                lambda ctx, name, next_step: 99 if name == "virtual" else next_step(),
            )
            root.on(
                "internal/set",
                lambda ctx, name, value, next_step: None if value == 2 else next_step(),
            )
            self.assertEqual(root.virtual, 99)
            root.value = 2
            self.assertEqual(root.value, 1)
            root.value = 3
            self.assertEqual(root.value, 3)
            callbacks = []

            def intercept(ctx, name, callback, options):
                if name == "intercepted":
                    callbacks.append(callback)
                    return callbacks.clear

            root.on("internal/listener", intercept)
            handle = root.on("intercepted", lambda: None)
            self.assertEqual(len(callbacks), 1)
            await handle()
            self.assertEqual(callbacks, [])


class LoaderParityTests(unittest.IsolatedAsyncioTestCase):
    async def test_closed_loader_releases_root_hooks_and_can_be_reused(self):
        async with Context() as root:
            loader = Loader(root)
            await loader.reconcile(
                [Entry("worker", "tests.sample_plugins:configured", 1)]
            )
            await loader.close()
            await loader.reconcile(
                [Entry("worker", "tests.sample_plugins:configured", 2)]
            )
            fiber = loader.get("worker")
            await fiber.update(3)
            self.assertEqual(loader.entries[0].config, 3)
            await loader.close()
            reference = weakref.ref(loader)
            del loader
            gc.collect()
            self.assertIsNone(reference())

    async def test_removed_patch_target_stays_removed_after_reload(self):
        async with Context() as root:
            backend, loader = MemoryBackend(), Loader(root)
            store = ConfigStore(
                loader,
                backend,
                JsonCodec(),
                patches=[ConfigPatch("worker", {"config": 2})],
            )
            await store.load()
            await store.remove("worker")
            await store.load()
            self.assertEqual(store.tree.entries, ())
            self.assertEqual(store.patches, ())

    async def test_cancelled_update_finishes_configuration_commit(self):
        entered, release = asyncio.Event(), asyncio.Event()

        class Backend(MemoryBackend):
            async def write(self, text):
                entered.set()
                await release.wait()
                await super().write(text)

        async with Context() as root:
            backend, loader = Backend(), Loader(root)
            store = ConfigStore(loader, backend, JsonCodec())
            await store.load()
            update = asyncio.create_task(loader.get("worker").update(2))
            await entered.wait()
            update.cancel()
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await update
            self.assertEqual(
                (
                    store.tree.get("worker").config,
                    loader.get("worker").config,
                    json.loads(backend.text)[0]["config"],
                ),
                (2, 2, 2),
            )

    async def test_concurrent_runtime_updates_preserve_both_entries(self):
        entered, release = asyncio.Event(), asyncio.Event()

        class Backend(MemoryBackend):
            async def write(self, text):
                entered.set()
                await release.wait()
                await super().write(text)

        async with Context() as root:
            backend, loader = Backend(), Loader(root)
            backend.text = '[{"id":"left","plugin":"tests.sample_plugins:configured","config":1},{"id":"right","plugin":"tests.sample_plugins:configured","config":1}]'
            store = ConfigStore(loader, backend, JsonCodec())
            await store.load()
            first = asyncio.create_task(loader.get("left").update(2))
            await entered.wait()
            second = asyncio.create_task(loader.get("right").update(3))
            release.set()
            await asyncio.gather(first, second)
            self.assertEqual([entry.config for entry in store.tree.entries], [2, 3])
            self.assertEqual(
                [entry["config"] for entry in json.loads(backend.text)], [2, 3]
            )

    async def test_watch_file_survives_invalid_edit_and_delete(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(
                '[{"id":"worker","plugin":"tests.sample_plugins:configured","config":1}]'
            )
            async with Context() as root:
                loader = Loader(root)
                await loader.load_file(path)
                ready, failed, applied, stop = (asyncio.Event() for _ in range(4))
                root.on(
                    "internal/status",
                    lambda fiber, old: (
                        applied.set()
                        if fiber.config == 2 and fiber.state is FiberState.ACTIVE
                        else None
                    ),
                )
                watcher = asyncio.create_task(
                    loader.watch_file(
                        path,
                        interval=0.001,
                        stop=stop,
                        ready=ready,
                        on_error=lambda error: failed.set(),
                    )
                )
                try:
                    await asyncio.wait_for(ready.wait(), 2)
                    path.write_text("invalid")
                    await asyncio.wait_for(failed.wait(), 2)
                    failed.clear()
                    path.unlink()
                    await asyncio.wait_for(failed.wait(), 2)
                    path.write_text(
                        '[{"id":"worker","plugin":"tests.sample_plugins:configured","config":2}]'
                    )
                    await asyncio.wait_for(applied.wait(), 2)
                    self.assertFalse(watcher.done())
                finally:
                    stop.set()
                    await watcher

    async def test_entry_inject_and_expression(self):
        seen = []

        @plugin
        def worker(ctx, config):
            seen.append((ctx.db, config, ctx.metadata("db")))

        async with Context() as root:
            loader = Loader(root.extend(port=8000))
            await loader.reconcile(
                [
                    Entry(
                        "worker",
                        worker,
                        {"port": ConfigExpression("port + 1")},
                        inject={"db": {"readonly": True}},
                    )
                ]
            )
            self.assertEqual(loader.get("worker").state, FiberState.PENDING)
            root.provide("db", "ready")
            await root.flush()
            self.assertEqual(seen, [("ready", {"port": 8001}, {"readonly": True})])
            with self.assertRaises(ValueError):
                ConfigExpression('__import__("os")').evaluate(root)

    async def test_fiber_update_and_disposal_persist(self):
        async with Context() as root:
            backend, loader = MemoryBackend(), Loader(root)
            store = ConfigStore(loader, backend, JsonCodec())
            await store.load()
            fiber = loader.get("worker")
            await fiber.update(2)
            self.assertEqual(
                (
                    store.tree.get("worker").config,
                    loader.entries[0].config,
                    json.loads(backend.text)[0]["config"],
                ),
                (2, 2, 2),
            )
            await fiber.dispose()
            self.assertFalse(store.tree.get("worker").enabled)
            self.assertFalse(json.loads(backend.text)[0]["enabled"])

    async def test_runtime_update_rolls_back_on_write_failure(self):
        async with Context() as root:
            backend, loader = MemoryBackend(fail=True), Loader(root)
            store = ConfigStore(loader, backend, JsonCodec())
            await store.load()
            fiber = loader.get("worker")
            with self.assertRaisesRegex(OSError, "read only"):
                await fiber.update(2)
            self.assertEqual(
                (
                    fiber.config,
                    store.tree.get("worker").config,
                    loader.entries[0].config,
                ),
                (1, 1, 1),
            )

    async def test_loader_ready_gate(self):
        started, release = asyncio.Event(), asyncio.Event()

        async def slow(ctx, _):
            started.set()
            await release.wait()

        async with Context() as root:
            loader = Loader(root)
            loader.provide()
            task = asyncio.create_task(
                loader.reconcile([Entry("g", children=(Entry("slow", slow),))])
            )
            await started.wait()
            consumer = root.intercept("loader", {"await": True}).inject(
                ["loader"], lambda ctx, _: None
            )
            self.assertEqual(consumer.state, FiberState.PENDING)
            release.set()
            await task
            await loader.wait_ready()
            self.assertEqual(consumer.state, FiberState.ACTIVE)

    async def test_patch_ownership(self):
        with tempfile.TemporaryDirectory() as directory:
            path, patches = (
                Path(directory) / name for name in ("config.json", "patches.json")
            )
            path.write_text(
                '[{"id":"base","plugin":"tests.sample_plugins:configured","config":1}]'
            )
            async with Context() as root:
                loader = Loader(root)
                store = ConfigStore.file(
                    loader,
                    path,
                    patches=[
                        ConfigPatch("base", {"config": 2}),
                        ConfigPatch(
                            insert=(
                                Entry("extra", "tests.sample_plugins:configured", 3),
                            )
                        ),
                    ],
                    patch_path=patches,
                )
                await store.load()
                await loader.get("base").update(4)
                await loader.get("extra").update(5)
                self.assertEqual(json.loads(path.read_text())[0]["config"], 1)
                self.assertEqual(len(json.loads(path.read_text())), 1)
                saved = json.loads(patches.read_text())
                self.assertEqual(
                    (saved[0]["changes"]["config"], saved[1]["insert"][0]["config"]),
                    (4, 5),
                )
                await store.load()
                self.assertEqual(
                    (store.tree.get("base").config, store.tree.get("extra").config),
                    (4, 5),
                )

    async def test_journal_retry_merges_file_edits(self):
        async with Context() as root:
            backend, loader = MemoryBackend(fail=True), Loader(root)
            store = ConfigStore(
                loader, backend, JsonCodec(), deferred=True, conflicts="file_wins"
            )
            await store.load()
            await store.update("worker", config=2)
            self.assertTrue(store.journal)
            backend.text = '[{"id":"worker","plugin":"tests.sample_plugins:configured","config":1,"intercept":{"x":true}}]'
            backend.fail = False
            await store.flush()
            result = json.loads(backend.text)[0]
            self.assertEqual((result["config"], result["intercept"]), (2, {"x": True}))
            self.assertFalse(store.journal)

    async def test_conflicting_file_field_wins(self):
        base = EntryTree((Entry("worker", config=1),))
        merged, conflicts = merge_trees(
            base, base.update("worker", config=2), base.update("worker", config=3)
        )
        self.assertEqual(merged.get("worker").config, 3)
        self.assertEqual(
            [(c.path, c.field) for c in conflicts], [(("worker",), "config")]
        )


class TimerParityTests(unittest.IsolatedAsyncioTestCase):
    async def test_timeout_without_callback(self):
        async with Context() as root:
            self.assertIsNone(await root.timer.timeout(0))

    async def test_tick_stream_close_and_context_disposal(self):
        async with Context() as root:
            async with root.timer.interval(0.001) as ticks:
                self.assertIsNone(await anext(ticks))
            with self.assertRaises(StopAsyncIteration):
                await anext(ticks)
            ticks = root.timer.interval(60)
            waiting = asyncio.create_task(anext(ticks))
            await asyncio.sleep(0)
            await root.close()
            with self.assertRaises(InactiveContextError):
                await waiting

    async def test_throttle_without_trailing(self):
        async with Context() as root:
            seen = []
            throttle = root.timer.throttle(0.01, seen.append, trailing=False)
            await throttle(1)
            self.assertIsNone(await throttle(2))
            await asyncio.sleep(0.02)
            await throttle(3)
            self.assertEqual(seen, [1, 3])


class HMRParityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name)
        self.module = "cordis_parity_" + uuid4().hex
        self.path = self.directory / (self.module + ".py")
        sys.path.insert(0, str(self.directory))
        self.write("v1")

    async def asyncTearDown(self):
        sys.modules.pop(self.module, None)
        sys.path.remove(str(self.directory))
        self.temporary.cleanup()

    def write(self, version, *, fail=False):
        failure = (
            "if config == 'fail': raise ValueError('broken replacement')"
            if fail
            else "pass"
        )
        self.path.write_text(f"""from cordis import plugin
@plugin
def app(ctx, config):
    {failure}
    ctx.provide('version', {version!r})
""")
        importlib.invalidate_caches()

    async def mount(self, loader):
        component = import_plugin(self.module + ":app")
        await loader.mount("typed", component.configure(None))
        await loader.reconcile(
            [
                Entry(
                    "g",
                    isolate=("version",),
                    children=(Entry("nested", self.module + ":app", "fail"),),
                )
            ]
        )

    async def test_nested_and_typed_mount_reload_transaction(self):
        async with Context() as root:
            loader = Loader(root)
            await self.mount(loader)
            self.write("v2")
            changed = await loader.reload_changed([self.path])
            self.assertEqual(set(changed), {"typed", "g/nested"})
            self.assertEqual(root.get("version"), "v2")
            self.assertEqual(loader.child("g").get("nested").ctx.get("version"), "v2")

    async def test_nested_failure_restores_typed_mount_and_module(self):
        async with Context() as root:
            loader = Loader(root)
            await self.mount(loader)
            previous = sys.modules[self.module]
            self.write("v2", fail=True)
            with self.assertRaises(ExceptionGroup):
                await loader.reload_changed([self.path])
            self.assertIs(sys.modules[self.module], previous)
            self.assertEqual(root.get("version"), "v1")
            self.assertEqual(loader.child("g").get("nested").ctx.get("version"), "v1")
            self.assertEqual(loader.get("typed").state, FiberState.ACTIVE)

    async def test_watcher_survives_syntax_error(self):
        from cordis import HotReloader

        async with Context() as root:
            loader = Loader(root)
            await loader.reconcile([Entry("app", self.module + ":app")])
            ready, failed, applied, stop = (asyncio.Event() for _ in range(4))
            root.on(
                "internal/status",
                lambda fiber, old: (
                    applied.set()
                    if fiber.state is FiberState.ACTIVE and root.get("version") == "v2"
                    else None
                ),
            )
            watcher = asyncio.create_task(
                HotReloader(loader).watch(
                    [self.directory],
                    interval=0.001,
                    stop=stop,
                    ready=ready,
                    on_error=lambda error: failed.set(),
                )
            )
            try:
                await asyncio.wait_for(ready.wait(), 2)
                self.path.write_text("def invalid(")
                await asyncio.wait_for(failed.wait(), 2)
                self.assertEqual(root.get("version"), "v1")
                self.write("v2")
                await asyncio.wait_for(applied.wait(), 2)
                self.assertFalse(watcher.done())
            finally:
                stop.set()
                await watcher
