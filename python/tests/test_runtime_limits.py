import asyncio
import inspect
import unittest
import weakref

from cordis import (
    Context,
    FiberState,
    InactiveContextError,
    ReadinessError,
    ReadinessTimeoutError,
    ShutdownTimeoutError,
    TaskLimitError,
    UndeclaredServiceError,
    plugin,
)


class RuntimeLimitTests(unittest.IsolatedAsyncioTestCase):
    def context(self, **options):
        root = Context(**options)
        self.addAsyncCleanup(root.close)
        return root

    async def test_task_limit_is_shared_by_plugins_and_scopes(self):
        root = self.context(max_tasks=2)
        gate = asyncio.Event()
        self.addCleanup(gate.set)
        first = root.spawn(gate.wait(), policy="cancel")

        @plugin
        def worker(ctx, _config):
            ctx.spawn(gate.wait(), policy="cancel")

        await root.use(worker)
        self.assertEqual(root.task_count, 2)
        async with root.scope() as scoped:
            rejected = gate.wait()
            with self.assertRaises(TaskLimitError):
                scoped.spawn(rejected)
            self.assertEqual(inspect.getcoroutinestate(rejected), inspect.CORO_CLOSED)
        first.cancel()
        await asyncio.gather(first, return_exceptions=True)
        self.assertEqual(root.task_count, 1)
        replacement = root.spawn(asyncio.sleep(0, result=42))
        self.assertEqual(await replacement, 42)

    async def test_saturated_event_dispatch_reports_rejection_without_leaking(self):
        root = self.context(max_tasks=1, error_history_limit=2)
        gate = asyncio.Event()
        self.addCleanup(gate.set)
        root.spawn(gate.wait(), policy="cancel")

        async def listener(*_args):
            self.fail("rejected listener must never run")

        root.on("message", listener)
        root.on("internal/error", listener)
        with self.assertRaises(TaskLimitError):
            root.emit("message")

        @plugin
        def broken(_ctx, _config):
            raise ValueError("startup failed while full")

        with self.assertRaises(ValueError):
            await root.use(broken)
        self.assertEqual(root.task_count, 1)
        self.assertEqual(root.error_count, 2)
        self.assertIsInstance(root.errors[1][0], TaskLimitError)

    async def test_bounded_error_history_notifies_every_failure_and_releases_owners(self):
        root = self.context(error_history_limit=2)
        observed = []
        root.on("internal/error", lambda error, _fiber: observed.append(str(error)))
        owners = []

        @plugin
        def failing(_ctx, number):
            raise ValueError(str(number))

        for number in range(5):
            fiber = root.use(failing, number)
            owners.append(weakref.ref(fiber))
            with self.assertRaises(ValueError):
                await fiber
            await fiber.dispose()
        del fiber
        # Exceptions retain tracebacks, including their Fiber. Eviction must
        # release those cycles so a long-running runtime can reclaim them.
        import gc

        gc.collect()
        self.assertTrue(all(owner() is None for owner in owners[:3]))
        self.assertEqual([str(error) for error, _ in root.errors], ["3", "4"])
        self.assertEqual(observed, [str(number) for number in range(5)])
        self.assertEqual(root.error_count, 5)
        root.clear_errors()
        self.assertEqual(root.errors, ())
        self.assertEqual(root.error_count, 5)

    async def test_error_history_can_be_disabled(self):
        root = self.context(error_history_limit=0)

        async def fail():
            raise ValueError("task failed")

        with self.assertRaises(ValueError):
            await root.spawn(fail())
        self.assertEqual(root.errors, ())
        self.assertEqual(root.error_count, 1)

    async def test_sync_once_works_when_business_task_capacity_is_exhausted(self):
        root = self.context(max_tasks=1)
        gate = asyncio.Event()
        self.addCleanup(gate.set)
        root.spawn(gate.wait(), policy="cancel")
        seen = []
        handle = root.once("message", lambda: seen.append("called"))
        root.emit("message")
        root.emit("message")
        await handle()
        self.assertEqual(seen, ["called"])
        self.assertEqual(root.task_count, 1)

    @unittest.skipUnless(hasattr(asyncio, "eager_task_factory"), "requires Python 3.12+")
    async def test_eager_tasks_reserve_capacity_before_executing(self):
        root = self.context(max_tasks=1)
        loop = asyncio.get_running_loop()
        factory = loop.get_task_factory()
        loop.set_task_factory(asyncio.eager_task_factory)
        try:
            async def parent():
                with self.assertRaises(TaskLimitError):
                    root.spawn(asyncio.sleep(0))
                return 42

            self.assertEqual(await root.spawn(parent()), 42)
        finally:
            loop.set_task_factory(factory)

    async def test_readiness_rejects_waiting_from_managed_work(self):
        root = self.context()

        async def work():
            with self.assertRaises(InactiveContextError):
                await root.wait_ready()

        await root.spawn(work())

    async def test_shutdown_timeout_preserves_task_and_resource_order(self):
        root = self.context()
        started = asyncio.Event()
        release = asyncio.Event()
        order = []
        self.addCleanup(release.set)

        @plugin
        def worker(ctx, _config):
            async def work():
                started.set()
                await release.wait()
                order.append("work")

            ctx.spawn(work(), name="blocked-query")
            ctx.defer(lambda: order.append("resource"))

        fiber = await root.use(worker)
        await started.wait()
        with self.assertRaises(ShutdownTimeoutError) as caught:
            await root.close(timeout=0)
        blocker = next(item for item in caught.exception.blockers if item.uid == fiber.uid)
        self.assertIn("blocked-query", blocker.tasks)
        self.assertIn("worker", str(caught.exception))
        self.assertEqual(order, [])
        self.assertEqual(root.task_count, 1)
        release.set()
        await root.close(timeout=None)
        await root.close(timeout=0)
        self.assertEqual(order, ["work", "resource"])

    async def test_shutdown_timeout_reports_stalled_cleanup_and_can_be_retried(self):
        root = self.context()
        started = asyncio.Event()
        release = asyncio.Event()
        calls = []
        self.addCleanup(release.set)

        async def cleanup():
            calls.append("started")
            started.set()
            await release.wait()
            calls.append("done")

        root.defer(cleanup)
        closing = asyncio.create_task(root.close(timeout=None))
        await started.wait()
        with self.assertRaises(ShutdownTimeoutError) as caught:
            await root.close(timeout=0)
        self.assertEqual(caught.exception.blockers[0].name, "root")
        self.assertEqual(caught.exception.blockers[0].state, FiberState.UNLOADING)
        self.assertFalse(closing.done())
        closing.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await closing
        release.set()
        await root.close()
        self.assertEqual(calls, ["started", "done"])

    async def test_shutdown_failure_after_timeout_is_still_reported(self):
        root = Context()
        release = asyncio.Event()
        self.addCleanup(release.set)

        async def cleanup():
            await release.wait()
            raise ValueError("late cleanup failure")

        root.defer(cleanup)
        with self.assertRaises(ShutdownTimeoutError):
            await root.close(timeout=0)
        release.set()
        with self.assertRaises(ExceptionGroup):
            await root.close()
        self.assertTrue(root.errors)
        self.assertIs(root.errors[-1][1], root.fiber)

    async def test_readiness_detects_missing_dependencies_and_failed_startup(self):
        root = self.context()

        @plugin(inject=["database"])
        def consumer(_ctx, _config):
            pass

        fiber = root.use(consumer)
        with self.assertRaises(ReadinessError) as caught:
            await root.wait_ready()
        self.assertEqual(caught.exception.issues[0].dependencies, ("database",))
        self.assertEqual(caught.exception.issues[0].uid, fiber.uid)
        root.provide("database", object())
        await root.wait_ready()
        self.assertEqual(fiber.state, FiberState.ACTIVE)

        @plugin
        def failed(_ctx, _config):
            raise ValueError("bad configuration")

        broken = root.use(failed)
        with self.assertRaises(ReadinessError) as caught:
            await root.wait_ready()
        issue = next(item for item in caught.exception.issues if item.uid == broken.uid)
        self.assertIs(issue.error, broken.error)
        self.assertIn("bad configuration", str(caught.exception))

    async def test_readiness_reports_declared_cycles_without_crossing_realms(self):
        root = self.context()

        @plugin(name="left", inject=["b"], provide=["a"])
        def left(ctx, _config):
            ctx.provide("a", 1)

        @plugin(name="right", inject=["a"], provide=["b"])
        def right(ctx, _config):
            ctx.provide("b", 2)

        root.use(left)
        root.use(right)
        with self.assertRaises(ReadinessError) as caught:
            await root.wait_ready()
        self.assertTrue(caught.exception.cycles)
        self.assertIn("left", str(caught.exception))
        self.assertIn("right", str(caught.exception))

        isolated = self.context()
        isolated.isolate("a", "b").use(left)
        isolated.isolate("a", "b").use(right)
        with self.assertRaises(ReadinessError) as caught:
            await isolated.wait_ready()
        self.assertEqual(caught.exception.cycles, ())

    async def test_readiness_timeout_and_cancellation_leave_initialization_running(self):
        root = self.context()
        started = asyncio.Event()
        release = asyncio.Event()
        self.addCleanup(release.set)

        @plugin
        async def slow(_ctx, _config):
            started.set()
            await release.wait()

        fiber = root.use(slow)
        await started.wait()
        with self.assertRaises(ReadinessTimeoutError) as caught:
            await root.wait_ready(timeout=0)
        self.assertEqual(caught.exception.issues[0].state, FiberState.LOADING)
        entered = asyncio.Event()

        async def wait():
            entered.set()
            await root.wait_ready(timeout=None)

        waiting = asyncio.create_task(wait())
        await entered.wait()
        waiting.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await waiting
        release.set()
        await root.wait_ready()
        self.assertEqual(fiber.state, FiberState.ACTIVE)
        await root.close()
        with self.assertRaises(InactiveContextError):
            await root.wait_ready()

    async def test_repeated_lifecycle_releases_registrations_and_capacity(self):
        root = self.context(max_tasks=2)
        events = []
        cleanups = []

        @plugin(provide=["temporary"])
        def worker(ctx, number):
            ctx.provide("temporary", number)
            ctx.on("tick", lambda: events.append(number))
            ctx.spawn(asyncio.sleep(0), policy="cancel")
            ctx.defer(lambda: cleanups.append(number))

        for number in range(200):
            fiber = root.use(worker, number)
            await root.wait_ready()
            root.emit("tick")
            await fiber.dispose()
            self.assertEqual(root.task_count, 0)
            self.assertEqual(root.fibers, ())
            self.assertEqual(root.fiber.effects, ())
            with self.assertRaises(UndeclaredServiceError):
                root.service("temporary")
        root.emit("tick")
        self.assertEqual(events, list(range(200)))
        self.assertEqual(cleanups, list(range(200)))
        self.assertEqual(root.error_count, 0)

    async def test_invalid_limits_and_deadlines_fail_before_mutation(self):
        for options in (
            {"max_tasks": 0}, {"max_tasks": -1}, {"max_tasks": True},
            {"error_history_limit": -1}, {"error_history_limit": 1.5},
        ):
            with self.subTest(options=options), self.assertRaises(ValueError):
                Context(**options)
        root = self.context(max_tasks=None)
        for timeout in (-1, float("nan"), float("inf")):
            with self.subTest(timeout=timeout):
                with self.assertRaises(ValueError):
                    await root.close(timeout=timeout)
                with self.assertRaises(ValueError):
                    await root.wait_ready(timeout=timeout)
        self.assertEqual(root.fiber.state, FiberState.ACTIVE)
