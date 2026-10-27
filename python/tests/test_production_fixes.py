"""Regression coverage for production lifecycle and admission boundaries."""

import asyncio
import unittest
from unittest.mock import patch

from cordis import (
    ConfigIncludes,
    ConfigStore,
    Context,
    Entry,
    FiberState,
    InactiveContextError,
    JsonCodec,
    Loader,
    ShutdownTimeoutError,
    TaskLimitError,
    plugin,
)


class MemoryBackend:
    identity = "production-test"

    def __init__(self, text="[]"):
        self.text = text
        self.writes = 0

    async def read(self):
        return self.text

    async def write(self, text):
        self.writes += 1
        self.text = text


class UpdateShutdownTests(unittest.IsolatedAsyncioTestCase):
    async def test_shutdown_waits_for_update_before_releasing_resources(self):
        entered, release = asyncio.Event(), asyncio.Event()
        order = []

        async def update(ctx, previous, config):
            entered.set()
            await release.wait()
            order.append("updated")

        @plugin(update=update)
        def worker(ctx, config):
            ctx.defer(lambda: order.append("closed"))

        root = Context()
        loader = Loader(root)
        await loader.reconcile([Entry("worker", worker, 0)])
        updating = asyncio.create_task(loader.reconcile([Entry("worker", worker, 1)]))
        try:
            await entered.wait()
            with self.assertRaises(ShutdownTimeoutError) as caught:
                await root.close(timeout=0.01)
            self.assertEqual(order, [])
            self.assertTrue(any(
                "cordis:update:worker" in blocker.tasks
                for blocker in caught.exception.blockers
            ))
        finally:
            release.set()
            await updating
            await root.close()
        self.assertEqual(order, ["updated", "closed"])

    async def test_dependency_disposal_waits_for_consumer_update(self):
        entered, release = asyncio.Event(), asyncio.Event()
        resource = {"closed": False}
        seen = []

        @plugin(provide=["db"])
        def database(ctx, config):
            ctx.provide("db", resource)
            ctx.defer(lambda: resource.update(closed=True))

        async def update(ctx, previous, config):
            database = ctx.service("db")
            entered.set()
            await release.wait()
            seen.append(database["closed"])

        @plugin(inject=["db"], update=update)
        def worker(ctx, config):
            pass

        async with Context() as root:
            provider = await root.use(database)
            consumer = await root.use(worker, 0)
            updating = asyncio.create_task(consumer.update(1))
            await entered.wait()
            disposing = asyncio.create_task(provider.dispose())
            try:
                with self.assertRaises(TimeoutError):
                    async with asyncio.timeout(0.01):
                        await asyncio.shield(disposing)
            finally:
                release.set()
            await asyncio.gather(updating, disposing)
            self.assertEqual(seen, [False])
            self.assertTrue(resource["closed"])

    async def test_update_hook_cannot_wait_for_its_own_disposal(self):
        async def update(ctx, previous, config):
            with self.assertRaises(InactiveContextError):
                await ctx.fiber.dispose()

        @plugin(update=update)
        def worker(ctx, config):
            pass

        async with Context() as root:
            fiber = await root.use(worker, 0)
            async with asyncio.timeout(1):
                await fiber.update(1)
            self.assertEqual(fiber.state, FiberState.ACTIVE)

    async def test_failed_hook_unblocks_shutdown_after_repeated_cancellation(self):
        entered, release = asyncio.Event(), asyncio.Event()
        order = []

        async def update(ctx, previous, config):
            entered.set()
            await release.wait()
            order.append("hook finished")
            raise ValueError("update failed")

        @plugin(update=update)
        def worker(ctx, config):
            ctx.defer(lambda: order.append("resource closed"))

        root = Context()
        fiber = await root.use(worker, 0)
        updating = asyncio.create_task(fiber.update(1))
        try:
            await entered.wait()
            updating.cancel()
            with self.assertRaises(ShutdownTimeoutError):
                await root.close(timeout=0.01)
            updating.cancel()
            self.assertEqual(order, [])
        finally:
            release.set()
            with self.assertRaises(asyncio.CancelledError) as caught:
                await updating
            await root.close()
        self.assertIsInstance(caught.exception.__cause__, ValueError)
        self.assertEqual(order, ["hook finished", "resource closed"])
        self.assertEqual(fiber.config, 0)

    async def test_queued_update_is_rejected_after_shutdown_begins(self):
        entered, release, queued = asyncio.Event(), asyncio.Event(), asyncio.Event()
        seen = []

        async def update(ctx, previous, config):
            seen.append((previous, config))
            if config == 1:
                entered.set()
                await release.wait()

        @plugin(update=update)
        def worker(ctx, config):
            pass

        root = Context()
        fiber = await root.use(worker, 0)
        first = asyncio.create_task(fiber.update(1))
        await entered.wait()

        async def next_update():
            queued.set()
            await fiber.update(2)

        second = asyncio.create_task(next_update())
        try:
            await queued.wait()
            with self.assertRaises(ShutdownTimeoutError):
                await root.close(timeout=0.01)
            with self.assertRaises(InactiveContextError):
                await fiber.update(3)
        finally:
            release.set()
            await first
            with self.assertRaises(InactiveContextError):
                await second
            await root.close()
        self.assertEqual(seen, [(0, 1)])
        self.assertEqual(fiber.config, 1)

    async def test_hook_flush_rejects_waiting_for_its_own_unload(self):
        entered, release = asyncio.Event(), asyncio.Event()

        async def update(ctx, previous, config):
            entered.set()
            await release.wait()
            with self.assertRaises(InactiveContextError):
                await ctx.flush()

        @plugin(update=update)
        def worker(ctx, config):
            pass

        root = Context()
        fiber = await root.use(worker, 0)
        updating = asyncio.create_task(fiber.update(1))
        try:
            await entered.wait()
            with self.assertRaises(ShutdownTimeoutError):
                await root.close(timeout=0.01)
        finally:
            release.set()
            async with asyncio.timeout(1):
                await updating
                await root.close()

    async def test_direct_update_hooks_are_serial(self):
        entered, release, queued = asyncio.Event(), asyncio.Event(), asyncio.Event()
        seen = []

        async def update(ctx, previous, config):
            seen.append((previous, config))
            if config == 1:
                entered.set()
                await release.wait()

        @plugin(update=update)
        def worker(ctx, config):
            pass

        async with Context() as root:
            fiber = await root.use(worker, 0)
            first = asyncio.create_task(fiber.update(1))
            await entered.wait()

            async def next_update():
                queued.set()
                await fiber.update(2)

            second = asyncio.create_task(next_update())
            try:
                await queued.wait()
            finally:
                release.set()
            await asyncio.gather(first, second)
            self.assertEqual(seen, [(0, 1), (1, 2)])
            self.assertEqual(fiber.config, 2)


class ManagementGuardTests(unittest.IsolatedAsyncioTestCase):
    async def test_registry_rejects_entire_batch_before_self_teardown(self):
        for operation in ("dispose", "restart"):
            with self.subTest(operation=operation):
                @plugin
                def worker(ctx, config):
                    pass

                async with Context() as root:
                    unrelated = await root.use(worker)
                    owner = await root.use(worker)

                    async def request():
                        with self.assertRaises(InactiveContextError):
                            await getattr(root.registry, operation)(worker)

                    async with asyncio.timeout(1):
                        await owner.ctx.spawn(request())
                    self.assertEqual(unrelated.state, FiberState.ACTIVE)
                    self.assertEqual(owner.state, FiberState.ACTIVE)

    async def test_config_store_checks_managed_caller_before_shielding(self):
        @plugin
        def worker(ctx, config):
            pass

        async with Context() as root:
            loader = Loader(root)
            await loader.reconcile([Entry("worker", worker, 0)])
            backend = MemoryBackend()
            store = ConfigStore(loader, backend, JsonCodec())
            fiber = loader.get("worker")

            async def request():
                with self.assertRaises(InactiveContextError):
                    await store.replace(store.tree, persist=False)

            await fiber.ctx.spawn(request())
            self.assertEqual(fiber.state, FiberState.ACTIVE)
            self.assertEqual(backend.writes, 0)

    async def test_config_includes_checks_caller_before_creating_group(self):
        @plugin
        def worker(ctx, config):
            pass

        async with Context() as root:
            loader = Loader(root)
            await loader.reconcile([Entry("worker", worker)])
            owner = loader.get("worker")
            includes = ConfigIncludes(loader)

            async def request():
                with self.assertRaises(InactiveContextError):
                    await includes.include("new", MemoryBackend(), JsonCodec())

            await owner.ctx.spawn(request())
            self.assertEqual(includes.names, ())
            self.assertEqual(tuple(entry.id for entry in loader.entries), ("worker",))


class NestedRollbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_unchanged_nested_group_preserves_typed_mount_on_rollback(self):
        @plugin(provide=["value"])
        def provider(ctx, config):
            ctx.provide("value", config)

        @plugin(inject=["value"])
        def consumer(ctx, config):
            if ctx.service("value") == "new":
                raise ValueError("incompatible provider")

        async with Context() as root:
            loader = Loader(root)
            original = [Entry("provider", provider, "old"), Entry("group")]
            await loader.reconcile(original)
            child = loader.child("group")
            mounted = await child.mount("typed", consumer.configure(None))
            with self.assertRaisesRegex(ValueError, "incompatible provider"):
                await loader.reconcile([
                    Entry("provider", provider, "new"), Entry("group")
                ])
            self.assertIs(loader.child("group"), child)
            self.assertIs(loader.get("group/typed"), mounted)
            self.assertEqual(mounted.state, FiberState.ACTIVE)
            self.assertEqual(root.get("value"), "old")


class TimerAdmissionTests(unittest.IsolatedAsyncioTestCase):
    @unittest.skipUnless(hasattr(asyncio, "eager_task_factory"), "requires Python 3.12+")
    async def test_eager_throttle_keeps_reentrant_trailing_call(self):
        release = asyncio.Event()
        seen, trailing = [], []

        async def sleep(delay):
            await release.wait()

        def callback(value):
            seen.append(value)
            if value == "leading":
                trailing.append(throttle("trailing"))

        async with Context(max_tasks=2) as root:
            throttle = root.timer.throttle(3600, callback)
            loop = asyncio.get_running_loop()
            factory = loop.get_task_factory()
            loop.set_task_factory(asyncio.eager_task_factory)
            try:
                with patch("cordis.timer.asyncio.sleep", sleep):
                    leading = throttle("leading")
                    self.assertEqual(len(trailing), 1)
                    release.set()
                    await asyncio.gather(leading, *trailing)
                self.assertEqual(seen, ["leading", "trailing"])
            finally:
                release.set()
                loop.set_task_factory(factory)

    async def test_rejected_throttle_leading_does_not_start_cooldown(self):
        async with Context(max_tasks=1) as root:
            gate = asyncio.Event()
            blocker = root.spawn(gate.wait(), policy="cancel")
            seen = []
            throttle = root.timer.throttle(3600, seen.append)
            try:
                with self.assertRaises(TaskLimitError):
                    throttle("rejected")
            finally:
                gate.set()
                await blocker
            async with asyncio.timeout(1):
                await throttle("admitted")
            self.assertEqual(seen, ["admitted"])

    async def test_rejected_debounce_keeps_admitted_call(self):
        async with Context(max_tasks=1) as root:
            seen = []
            debounce = root.timer.debounce(0, seen.append)
            first = debounce("first")
            with self.assertRaises(TaskLimitError):
                debounce("rejected")
            await first
            self.assertEqual(seen, ["first"])

    async def test_admitted_debounce_replaces_previous_call(self):
        async with Context(max_tasks=2) as root:
            seen = []
            debounce = root.timer.debounce(0, seen.append)
            first = debounce("first")
            last = debounce("last")
            await asyncio.gather(first, last, return_exceptions=True)
            self.assertTrue(first.cancelled())
            self.assertEqual(seen, ["last"])

    async def test_rejected_throttle_trailing_keeps_previous_call(self):
        entered, release = asyncio.Event(), asyncio.Event()

        async def sleep(delay):
            entered.set()
            await release.wait()

        async with Context(max_tasks=1) as root:
            seen = []
            throttle = root.timer.throttle(3600, seen.append)
            await throttle("leading")
            with patch("cordis.timer.asyncio.sleep", sleep):
                first = throttle("trailing")
                try:
                    await entered.wait()
                    with self.assertRaises(TaskLimitError):
                        throttle("rejected")
                finally:
                    release.set()
                await first
            self.assertEqual(seen, ["leading", "trailing"])
