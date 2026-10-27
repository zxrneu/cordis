import asyncio
import unittest
from contextlib import asynccontextmanager, contextmanager

from cordis import Context, InactiveContextError, QueryKey, ServiceKey, plugin


class ResourceTests(unittest.IsolatedAsyncioTestCase):
    async def test_enter_context_returns_value_and_releases_in_reverse_order(self):
        order = []
        value = object()

        @contextmanager
        def resource(name):
            order.append(f"open:{name}")
            try:
                yield value
            finally:
                order.append(f"close:{name}")

        @plugin
        def worker(ctx, _config):
            self.assertIs(ctx.enter_context(resource("first")), value)
            ctx.enter_context(resource("second"))

        async with Context() as root:
            fiber = await root.use(worker)
            self.assertEqual(order, ["open:first", "open:second"])
            await fiber.dispose()
            await fiber.dispose()
        self.assertEqual(
            order, ["open:first", "open:second", "close:second", "close:first"]
        )

    async def test_enter_async_context_releases_after_owned_work_finishes(self):
        order = []
        started = asyncio.Event()
        value = object()

        @asynccontextmanager
        async def resource():
            try:
                yield value
            finally:
                order.append("closed")

        @plugin
        async def worker(ctx, _config):
            self.assertIs(await ctx.enter_async_context(resource()), value)

            async def work():
                started.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    order.append("finished")

            ctx.spawn(work(), policy="cancel")

        async with Context() as root:
            fiber = await root.use(worker)
            await started.wait()
            await fiber.dispose()
        self.assertEqual(order, ["finished", "closed"])

    async def test_failed_initialization_releases_acquired_resources(self):
        closed = []

        @contextmanager
        def resource():
            try:
                yield
            finally:
                closed.append(True)

        @plugin
        def worker(ctx, _config):
            ctx.enter_context(resource())
            raise ValueError("startup failed")

        async with Context() as root:
            with self.assertRaisesRegex(ValueError, "startup failed"):
                await root.use(worker)
        self.assertEqual(closed, [True])

    async def test_closed_context_rejects_acquisition_before_entering(self):
        entered = []

        @contextmanager
        def resource():
            entered.append(True)
            yield

        root = Context()
        await root.close()
        with self.assertRaises(InactiveContextError):
            root.enter_context(resource())
        self.assertEqual(entered, [])

    async def test_async_acquisition_racing_with_close_releases_resource(self):
        entered, release = asyncio.Event(), asyncio.Event()
        closed = []

        @asynccontextmanager
        async def resource():
            entered.set()
            await release.wait()
            try:
                yield object()
            finally:
                closed.append(True)

        root = Context()
        self.addAsyncCleanup(root.close)
        acquisition = asyncio.create_task(root.enter_async_context(resource()))
        await entered.wait()
        await root.close()
        release.set()
        with self.assertRaises(InactiveContextError):
            await acquisition
        self.assertEqual(closed, [True])

    async def test_scope_owns_only_registrations_made_through_its_context(self):
        seen = []
        async with Context() as root:
            async with root.scope() as scope:
                scope.on("event", lambda: seen.append("scoped"))
                root.on("event", lambda: seen.append("root"))
                root.emit("event")
            root.emit("event")
        self.assertEqual(seen, ["scoped", "root", "root"])

    async def test_scope_cancels_workers_before_releasing_resources(self):
        started = asyncio.Event()
        order = []

        async with Context() as root:
            async with root.scope() as scope:
                scope.defer(lambda: order.append("closed"))

                async def work():
                    started.set()
                    try:
                        await asyncio.Event().wait()
                    finally:
                        order.append("finished")

                task = scope.spawn(work(), policy="cancel")
                await started.wait()
            self.assertTrue(task.cancelled())
            self.assertEqual(root.fiber.tasks, ())
        self.assertEqual(order, ["finished", "closed"])

    async def test_scope_rejects_reuse_after_exit(self):
        async with Context() as root:
            async with root.scope() as scope:
                derived = scope.intercept("value", {})
            for ctx in (scope, derived):
                with self.assertRaises(InactiveContextError):
                    ctx.on("event", lambda: None)
                with self.assertRaises(InactiveContextError):
                    ctx.provide("value", 1)

    async def test_scope_cleans_up_on_error_and_preserves_the_error(self):
        closed = []
        async with Context() as root:
            with self.assertRaisesRegex(ValueError, "body failed"):
                async with root.scope() as scope:
                    scope.defer(lambda: closed.append(True))
                    raise ValueError("body failed")
        self.assertEqual(closed, [True])

    async def test_nested_scopes_and_legacy_effects_keep_their_ownership(self):
        closed = []
        async with Context() as root:
            async with root.scope() as outer:
                outer.defer(lambda: closed.append("outer"))
                async with outer.scope() as inner:
                    handle = await inner.effect(
                        lambda: inner.defer(lambda: closed.append("effect"))
                    )
                    await handle()
                    self.assertEqual(closed, ["effect"])
                    inner.defer(lambda: closed.append("inner"))
                self.assertEqual(closed, ["effect", "inner"])
        self.assertEqual(closed, ["effect", "inner", "outer"])

    async def test_parent_close_also_closes_an_open_scope(self):
        closed = []
        root = Context()
        async with root.scope() as scope:
            scope.defer(lambda: closed.append(True))
            await root.close()
            self.assertEqual(closed, [True])
        self.assertEqual(closed, [True])

    async def test_scope_disposes_child_plugins_and_query_handlers(self):
        closed = []
        query = QueryKey[int, int]("double")

        @plugin
        def child(ctx, _config):
            ctx.defer(lambda: closed.append("child"))

        async with Context() as root:
            async with root.scope() as scope:
                fiber = await scope.use(child)
                scope.handle(query, lambda value: value * 2)
                self.assertEqual(await root.query(query, 3), 6)
            self.assertEqual(fiber.state.value, "disposed")
            self.assertIsNone(await root.query(query, 3))
        self.assertEqual(closed, ["child"])

    async def test_cancelled_scope_waits_for_async_cleanup_despite_repeat_cancel(self):
        started, closing, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        closed = []

        @asynccontextmanager
        async def resource():
            try:
                yield
            finally:
                closing.set()
                await release.wait()
                closed.append(True)

        async with Context() as root:

            async def use():
                async with root.scope() as scope:
                    await scope.enter_async_context(resource())
                    started.set()
                    await asyncio.Event().wait()

            task = asyncio.create_task(use())
            await started.wait()
            task.cancel()
            await closing.wait()
            self.assertFalse(task.done())
            task.cancel()
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertEqual(closed, [True])

    async def test_cleanup_failure_does_not_skip_other_resources(self):
        closed = []

        @contextmanager
        def resource(name, *, fail=False):
            try:
                yield
            finally:
                closed.append(name)
                if fail:
                    raise ValueError("close failed")

        async with Context() as root:
            with self.assertRaises(ExceptionGroup):
                async with root.scope() as scope:
                    scope.enter_context(resource("first"))
                    scope.enter_context(resource("second", fail=True))
            self.assertEqual(closed, ["second", "first"])

    async def test_standard_contextmanager_plugin_keeps_resource_until_unload(self):
        order = []

        @plugin
        @contextmanager
        def worker(ctx, _config):
            order.append("open")
            try:
                yield
            finally:
                order.append("close")

        async with Context() as root:
            fiber = await root.use(worker)
            self.assertEqual(order, ["open"])
            await fiber.dispose()
        self.assertEqual(order, ["open", "close"])

    async def test_owned_task_cannot_close_its_own_scope(self):
        async with Context() as root:
            manager = root.scope()
            scope = await manager.__aenter__()

            async def close():
                await manager.__aexit__(None, None, None)

            task = scope.spawn(close())
            with self.assertRaises(InactiveContextError):
                await task

    async def test_cancelled_acquisition_preserves_manager_cancellation_cleanup(self):
        entered = asyncio.Event()
        closed = []

        @asynccontextmanager
        async def resource():
            try:
                entered.set()
                await asyncio.Event().wait()
                yield
            finally:
                closed.append(True)

        async with Context() as root:
            task = asyncio.create_task(root.enter_async_context(resource()))
            await entered.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertEqual(closed, [True])

    async def test_plain_service_retains_identity_and_explicit_owner(self):
        class Worker:
            def install(self, owner, seen):
                owner.on("event", seen.append)

        key = ServiceKey[Worker]("worker")
        service, seen = Worker(), []

        @plugin(provide=[key])
        def provider(ctx, _config):
            ctx.provide(key, service)

        @plugin(inject=[key])
        def consumer(ctx, _config):
            self.assertIs(ctx.service(key), service)
            service.install(ctx, seen)

        async with Context() as root:
            writer = await root.use(provider)
            reader = await root.use(consumer)
            root.emit("event", 1)
            await reader.dispose()
            root.emit("event", 2)
            self.assertIs(root.service(key), service)
            self.assertEqual(writer.state.value, "active")
        self.assertEqual(seen, [1])
