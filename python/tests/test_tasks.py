import asyncio
import inspect
import unittest

from cordis import Context, CordisError, FiberState, InactiveContextError, plugin


class TaskTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.root = Context()
        self.addAsyncCleanup(self.root.close)

    def gate(self):
        event = asyncio.Event()
        self.addCleanup(event.set)
        return event

    async def test_spawn_returns_result_and_releases_completed_tasks(self):
        async def answer():
            return 42

        task = self.root.spawn(answer(), name="answer")
        self.assertEqual(task.get_name(), "answer")
        self.assertEqual(await task, 42)
        self.assertEqual(self.root.fiber.tasks, ())
        self.assertEqual(self.root.fiber._scope.tasks, {})

        future = asyncio.get_running_loop().create_future()
        task = self.root.spawn(future)
        future.set_result("future")
        self.assertEqual(await task, "future")

    async def test_reload_drains_consumer_before_provider_cleanup(self):
        gate = self.gate()
        started = self.gate()
        unloading = self.gate()
        order = []
        tasks = []

        @plugin(provide=["database"])
        def database(ctx, version):
            ctx.provide("database", version)
            return lambda: order.append(f"close:{version}")

        @plugin(inject=["database"])
        def worker(ctx, _config):
            async def query():
                started.set()
                await gate.wait()
                order.append(f"query:{ctx.database}")

            tasks.append(ctx.spawn(query()))
            return lambda: order.append(f"stop:{ctx.database}")

        provider = await self.root.use(database, "v1")
        consumer = await self.root.use(worker)
        await started.wait()
        self.root.on(
            "internal/status",
            lambda fiber, _old: (
                unloading.set()
                if fiber is consumer and fiber.state is FiberState.UNLOADING
                else None
            ),
        )
        update = asyncio.create_task(provider.update("v2"))
        await unloading.wait()
        self.assertFalse(update.done())
        self.assertEqual(order, [])

        rejected = gate.wait()
        with self.assertRaises(InactiveContextError):
            consumer.ctx.spawn(rejected)
        self.assertEqual(inspect.getcoroutinestate(rejected), inspect.CORO_CLOSED)

        gate.set()
        await asyncio.wait_for(update, 1)
        await asyncio.gather(*tasks)
        self.assertEqual(order[:3], ["query:v1", "stop:v1", "close:v1"])
        self.assertIn("query:v2", order)
        self.assertEqual(consumer.state, FiberState.ACTIVE)

    async def test_cancel_waits_for_finally_before_releasing_resources(self):
        started = self.gate()
        cancelling = self.gate()
        release = self.gate()
        order = []
        tasks = []

        @plugin
        def worker(ctx, _config):
            async def work():
                started.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    cancelling.set()
                    await release.wait()
                    order.append("finally")

            tasks.append(ctx.spawn(work(), policy="cancel"))
            return lambda: order.append("cleanup")

        fiber = await self.root.use(worker)
        await started.wait()
        dispose = asyncio.create_task(fiber.dispose())
        await cancelling.wait()
        self.assertFalse(dispose.done())
        self.assertEqual(order, [])
        release.set()
        await asyncio.wait_for(dispose, 1)
        self.assertTrue(tasks[0].cancelled())
        self.assertEqual(order, ["finally", "cleanup"])
        self.assertEqual(fiber.tasks, ())
        self.assertEqual(self.root.errors, ())

    async def test_drain_timeout_cancels_and_waits(self):
        started = self.gate()
        finished = []

        async def work():
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                finished.append(True)

        task = self.root.spawn(work(), timeout=0.01)
        await started.wait()
        await asyncio.wait_for(self.root.close(), 1)
        self.assertTrue(task.cancelled())
        self.assertEqual(finished, [True])

    async def test_unload_cancels_all_workers_before_waiting_for_drains(self):
        started = self.gate()
        released = self.gate()
        tasks = []

        @plugin
        def worker(ctx, _config):
            async def cancelled():
                started.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    released.set()

            # The drain depends on the cancelled worker. Serial task cleanup
            # would deadlock if it waited for the drain first.
            tasks.append(ctx.spawn(released.wait()))
            tasks.append(ctx.spawn(cancelled(), policy="cancel"))

        fiber = await self.root.use(worker)
        await started.wait()
        await asyncio.wait_for(fiber.dispose(), 1)
        self.assertTrue(tasks[0].done())
        self.assertTrue(tasks[1].cancelled())

    async def test_failed_initialization_settles_tasks_before_rollback(self):
        started = self.gate()
        order = []

        @plugin
        async def broken(ctx, _config):
            async def work():
                started.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    order.append("task")

            ctx.spawn(work(), policy="cancel")
            await ctx.effect(lambda: lambda: order.append("resource"))
            await started.wait()
            raise RuntimeError("initialization failed")

        fiber = self.root.use(broken)
        with self.assertRaisesRegex(RuntimeError, "initialization failed"):
            await asyncio.wait_for(fiber.wait(), 1)
        self.assertEqual(order, ["task", "resource"])
        self.assertEqual(fiber.tasks, ())
        self.assertEqual(fiber.state, FiberState.FAILED)

    async def test_cancelled_effect_initialization_still_rolls_back(self):
        started = self.gate()
        order = []

        async def initialize():
            await self.root.effect(lambda: lambda: order.append("resource"))

            async def work():
                started.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    order.append("task")

            await self.root.spawn(work(), policy="cancel")

        initialize_task = asyncio.create_task(self.root.effect(initialize))
        await started.wait()
        await asyncio.wait_for(self.root.close(), 1)
        with self.assertRaisesRegex(CordisError, "cancelled"):
            await initialize_task
        self.assertEqual(order, ["task", "resource"])

    async def test_cancelled_cleanup_does_not_skip_remaining_inverses(self):
        root = Context()
        order = []

        @plugin
        def worker(_ctx, _config):
            async def cancelled():
                raise asyncio.CancelledError()

            yield lambda: order.append("last")
            yield cancelled

        fiber = await root.use(worker)
        with self.assertRaises(ExceptionGroup):
            await fiber.dispose()
        self.assertEqual(order, ["last"])
        self.assertEqual(fiber.state, FiberState.DISPOSED)
        # Root still holds the idempotent child disposer, which must keep
        # reporting that cleanup failure on subsequent shutdown.
        with self.assertRaises(ExceptionGroup):
            await root.close()

    async def test_nested_effect_disposal_settles_its_tasks_only(self):
        started = self.gate()
        order = []
        outside = self.root.spawn(self.gate().wait(), policy="cancel")
        tasks = []

        async def initialize():
            async def work():
                started.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    order.append("task")

            tasks.append(self.root.spawn(work(), policy="cancel"))
            await self.root.effect(lambda: lambda: order.append("resource"))

        handle = await self.root.effect(initialize)
        await started.wait()
        await asyncio.wait_for(handle(), 1)
        self.assertTrue(tasks[0].cancelled())
        self.assertFalse(outside.done())
        self.assertEqual(order, ["task", "resource"])

    async def test_concurrent_scope_disposal_does_not_cancel_finally_twice(self):
        started = self.gate()
        cancelling = self.gate()
        release = self.gate()
        finished = []

        def initialize():
            async def work():
                started.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    cancelling.set()
                    await release.wait()
                    finished.append(True)

            self.root.spawn(work(), policy="cancel")

        handle = await self.root.effect(initialize)
        await started.wait()
        dispose = asyncio.create_task(handle())
        await cancelling.wait()
        close = asyncio.create_task(self.root.close())
        await asyncio.sleep(0)
        release.set()
        await asyncio.wait_for(asyncio.gather(dispose, close), 1)
        self.assertEqual(finished, [True])

    async def test_task_failures_are_reported_once_with_owner(self):
        errors = []
        self.root.on(
            "internal/error", lambda error, fiber: errors.append((error, fiber))
        )

        async def broken():
            raise ValueError("background failure")

        task = self.root.spawn(broken())
        with self.assertRaisesRegex(ValueError, "background failure"):
            await task
        self.assertEqual(len(errors), 1)
        self.assertIs(errors[0][1], self.root.fiber)
        await self.root.close()
        self.assertEqual(tuple(errors), self.root.errors)

    async def test_rejected_and_unstarted_coroutines_are_closed(self):
        for options in (
            {"policy": "invalid"},
            {"timeout": -1},
            {"timeout": float("nan")},
            {"timeout": float("inf")},
        ):
            with self.subTest(options=options):
                coroutine = asyncio.sleep(0)
                with self.assertRaises(ValueError):
                    self.root.spawn(coroutine, **options)
                self.assertEqual(
                    inspect.getcoroutinestate(coroutine), inspect.CORO_CLOSED
                )

        coroutine = asyncio.sleep(0)
        task = self.root.spawn(coroutine)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        self.assertEqual(inspect.getcoroutinestate(coroutine), inspect.CORO_CLOSED)
        self.assertEqual(self.root.fiber.tasks, ())

        await self.root.close()
        coroutine = asyncio.sleep(0)
        with self.assertRaises(InactiveContextError):
            self.root.spawn(coroutine)
        self.assertEqual(inspect.getcoroutinestate(coroutine), inspect.CORO_CLOSED)

    async def test_emit_tracks_listener_owner_and_closes_admission_on_unload(self):
        gate = self.gate()
        started = self.gate()
        unloading = self.gate()
        seen = []

        @plugin(inject=["database"])
        def worker(ctx, _config):
            async def query(value):
                started.set()
                await gate.wait()
                seen.append((value, ctx.database))

            ctx.on("query", query)

        @plugin(provide=["database"])
        def database(ctx, _config):
            ctx.provide("database", "open")
            return lambda: seen.append("closed")

        provider = await self.root.use(database)
        consumer = await self.root.use(worker)
        self.root.emit("query", "first")
        await started.wait()
        self.assertEqual(len(consumer.tasks), 1)
        self.assertEqual(self.root.fiber.tasks, ())
        self.root.on(
            "internal/status",
            lambda fiber, _old: (
                unloading.set()
                if fiber is consumer and fiber.state is FiberState.UNLOADING
                else None
            ),
        )
        dispose = asyncio.create_task(provider.dispose())
        await unloading.wait()
        self.root.emit("query", "late")
        gate.set()
        await asyncio.wait_for(dispose, 1)
        self.assertEqual(seen, [("first", "open"), "closed"])
        self.assertEqual(self.root.errors, ())

    async def test_async_event_in_nested_effect_follows_registration_scope(self):
        started = self.gate()
        finished = []

        async def listener():
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                finished.append(True)

        def initialize():
            self.root.on("job", listener, task_policy="cancel")

        handle = await self.root.effect(initialize)
        self.root.emit("job")
        await started.wait()
        await asyncio.wait_for(handle(), 1)
        self.assertEqual(finished, [True])
        self.assertEqual(self.root.fiber.tasks, ())
        self.root.emit("job")
        self.assertEqual(self.root.fiber.tasks, ())

    async def test_event_callback_effects_do_not_attach_to_emitter(self):
        seen = []

        @plugin
        def receiver(ctx, _config):
            ctx.on("register", lambda: ctx.on("message", seen.append))

        @plugin
        def sender(ctx, _config):
            ctx.emit("register")

        receiver_fiber = await self.root.use(receiver)
        sender_fiber = await self.root.use(sender)
        await sender_fiber.dispose()
        self.root.emit("message", 1)
        await receiver_fiber.dispose()
        self.root.emit("message", 2)
        self.assertEqual(seen, [1])

    async def test_event_returning_managed_task_does_not_register_it_twice(self):
        async def broken():
            raise ValueError("once")

        @plugin
        def receiver(ctx, _config):
            ctx.on("job", lambda: ctx.spawn(broken()))

        fiber = await self.root.use(receiver)
        self.root.emit("job")
        self.assertEqual(len(fiber.tasks), 1)
        await asyncio.gather(*fiber.tasks, return_exceptions=True)
        self.assertEqual(len(self.root.errors), 1)
        self.assertIs(self.root.errors[0][1], fiber)

    async def test_once_consumes_only_one_of_consecutive_emits(self):
        seen = []
        self.root.once("message", seen.append)
        self.root.emit("message", 1)
        self.root.emit("message", 2)
        await asyncio.gather(*self.root.fiber.tasks)
        self.assertEqual(seen, [1])

    async def test_error_listener_failure_does_not_recurse(self):
        async def fail(_error, _fiber):
            raise RuntimeError("reporter failed")

        async def broken():
            raise ValueError("original")

        self.root.on("internal/error", fail)
        await asyncio.gather(self.root.spawn(broken()), return_exceptions=True)
        await self.root.close()
        self.assertEqual(
            [str(error) for error, _ in self.root.errors],
            ["original", "reporter failed"],
        )

    async def test_async_dispatch_strategies_preserve_results_and_scopes(self):
        seen = []
        left = self.root.isolate("value")
        right = self.root.isolate("value")

        async def record(value):
            seen.append(value)

        left.on("message", record)
        right.on("message", lambda value: seen.append("right"))
        await self.root.parallel("message", "left", scope=left)
        self.assertEqual(seen, ["left"])

        self.root.on("query", lambda: False)
        self.root.on("query", lambda: asyncio.sleep(0, result="answer"))
        self.root.on("query", lambda: self.fail("serial must short circuit"))
        self.assertEqual(await self.root.serial("query"), "answer")

        async def middleware(value, next_):
            return await next_(value + 1)

        self.root.on("pipeline", middleware)
        self.root.on("pipeline", lambda value, next_: next_(value * 2))
        result = self.root.waterfall(
            "pipeline", 3, final=lambda value: asyncio.sleep(0, result=value)
        )
        self.assertEqual(await result, 8)

        coroutine = asyncio.sleep(0)
        self.root.on("sync", lambda: coroutine)
        with self.assertRaisesRegex(TypeError, "synchronous"):
            self.root.bail("sync")
        self.assertEqual(inspect.getcoroutinestate(coroutine), inspect.CORO_CLOSED)

    async def test_closing_scope_rejects_new_effects_from_finishing_tasks(self):
        started = self.gate()
        rejected = []

        async def work():
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                for callback in (
                    lambda: self.root.provide("late", True),
                    lambda: self.root.on("late", lambda: None),
                    lambda: self.root.use(lambda ctx, config: None),
                ):
                    with self.assertRaises(InactiveContextError):
                        callback()
                    rejected.append(True)

        self.root.spawn(work(), policy="cancel")
        await started.wait()
        await self.root.close()
        self.assertEqual(rejected, [True, True, True])
        self.assertEqual(self.root.fibers, ())
        self.assertIsNone(self.root.get("late"))

    async def test_managed_task_cannot_wait_for_its_own_shutdown(self):
        async def work():
            with self.assertRaises(InactiveContextError):
                await self.root.close()

        await asyncio.wait_for(self.root.spawn(work()), 1)
        self.assertEqual(self.root.fiber.state, FiberState.ACTIVE)

    async def test_managed_task_cannot_dispose_a_dependency(self):
        @plugin(provide=["database"])
        def database(ctx, _config):
            ctx.provide("database", True)

        provider = await self.root.use(database)
        tasks = []

        @plugin(inject=["database"])
        def worker(ctx, _config):
            async def work():
                with self.assertRaises(InactiveContextError):
                    await provider.dispose()

            tasks.append(ctx.spawn(work()))

        await self.root.use(worker)
        await asyncio.wait_for(tasks[0], 1)
        self.assertEqual(provider.state, FiberState.ACTIVE)

    async def test_cancelling_a_close_waiter_does_not_abandon_shutdown(self):
        started = self.gate()
        cancelling = self.gate()
        release = self.gate()
        finished = []

        async def work():
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelling.set()
                await release.wait()
                finished.append(True)

        self.root.spawn(work(), policy="cancel")
        await started.wait()
        first = asyncio.create_task(self.root.close())
        await cancelling.wait()
        first.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await first
        second = asyncio.create_task(self.root.close())
        self.assertEqual(self.root.fiber.state, FiberState.UNLOADING)
        release.set()
        await asyncio.wait_for(second, 1)
        self.assertEqual(finished, [True])
        self.assertEqual(self.root.fiber.state, FiberState.DISPOSED)


if __name__ == "__main__":
    unittest.main()
