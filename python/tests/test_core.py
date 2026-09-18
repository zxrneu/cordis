import asyncio
import unittest

from cordis import (
    Context,
    FiberState,
    MissingProvisionError,
    UndeclaredServiceError,
    plugin,
)


class CordisTests(unittest.IsolatedAsyncioTestCase):
    async def test_effects_are_reverted_in_lifo_order(self):
        order = []

        @plugin
        async def sample(ctx, _config):
            await ctx.effect(lambda: lambda: order.append("first"))
            await ctx.effect(lambda: lambda: order.append("second"))

        root = Context()
        fiber = await root.use(sample)
        await fiber.dispose()
        await fiber.dispose()
        self.assertEqual(order, ["second", "first"])

    async def test_dependency_activates_and_deactivates_reactively(self):
        order = []

        @plugin(inject=["greeter"])
        def consumer(ctx, _config):
            self.assertEqual(ctx.greeter("Cordis"), "hello Cordis")
            order.append("consumer:start")
            return lambda: order.append("consumer:stop")

        @plugin
        def provider(ctx, _config):
            ctx.provide("greeter", lambda name: f"hello {name}")
            order.append("provider:start")
            return lambda: order.append("provider:stop")

        root = Context()
        consumer_fiber = root.use(consumer)
        await root.flush()
        self.assertEqual(consumer_fiber.state, FiberState.PENDING)

        provider_fiber = await root.use(provider)
        self.assertEqual(consumer_fiber.state, FiberState.ACTIVE)
        await provider_fiber.dispose()
        self.assertEqual(consumer_fiber.state, FiberState.PENDING)
        self.assertEqual(
            order,
            ["provider:start", "consumer:start", "consumer:stop", "provider:stop"],
        )

    async def test_nested_plugin_and_events_follow_parent_lifetime(self):
        seen = []

        @plugin
        def child(ctx, _config):
            ctx.on("message", seen.append)

        @plugin
        def parent(ctx, _config):
            ctx.use(child)

        root = Context()
        fiber = await root.use(parent)
        root.emit("message", 1)
        await fiber.dispose()
        root.emit("message", 2)
        self.assertEqual(seen, [1])

    async def test_isolated_services_do_not_collide(self):
        values = []

        @plugin
        def provider(ctx, value):
            ctx.provide("value", value)

        @plugin(inject=["value"])
        def consumer(ctx, _config):
            values.append(ctx["value"])

        root = Context()
        left = root.isolate("value")
        right = root.isolate("value")
        left.use(provider, "left")
        left.use(consumer)
        right.use(provider, "right")
        right.use(consumer)
        await root.flush()
        self.assertCountEqual(values, ["left", "right"])

    async def test_nested_isolation_inherits_and_overrides_realms(self):
        root = Context()
        realm = object()
        parent = root.isolate("value", realm=realm)

        self.assertIs(parent.intercept("value", None)._realm("value"), realm)
        self.assertIs(parent.isolate("other")._realm("value"), realm)
        self.assertIsNot(parent.isolate("value")._realm("value"), realm)

    async def test_undeclared_service_access_is_rejected(self):
        @plugin
        def bad(ctx, _config):
            _ = ctx.secret

        root = Context()
        fiber = root.use(bad)
        with self.assertRaises(UndeclaredServiceError):
            await fiber

    async def test_pending_plugin_can_be_disposed(self):
        @plugin(inject=["missing"])
        def pending(_ctx, _config):
            self.fail("pending plugin must not run")

        root = Context()
        fiber = root.use(pending)
        await fiber.dispose()
        self.assertEqual(fiber.state, FiberState.DISPOSED)
        self.assertNotIn(fiber, root.fibers)

    async def test_failed_plugin_can_restart(self):
        attempts = 0

        @plugin
        def flaky(_ctx, _config):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RuntimeError("first attempt")

        root = Context()
        fiber = root.use(flaky)
        with self.assertRaisesRegex(RuntimeError, "first attempt"):
            await fiber
        await fiber.restart()
        self.assertEqual(fiber.state, FiberState.ACTIVE)
        self.assertEqual(attempts, 2)

    async def test_partial_effect_is_rolled_back_when_iterator_fails(self):
        order = []

        def broken():
            yield lambda: order.append(1)
            yield lambda: order.append(2)
            raise RuntimeError("broken effect")

        root = Context()
        with self.assertRaisesRegex(RuntimeError, "broken effect"):
            await root.effect(broken)
        self.assertEqual(order, [2, 1])

    async def test_async_effect_stops_at_an_iteration_boundary(self):
        produced = []
        recovered = []
        started = asyncio.Event()

        async def stream():
            for index in range(100):
                produced.append(index)
                started.set()
                yield lambda index=index: recovered.append(index)
                await asyncio.sleep(0.001)

        root = Context()
        task = asyncio.create_task(root.effect(stream))
        await started.wait()
        await root.close()
        await task
        self.assertLess(len(produced), 100)
        self.assertEqual(recovered, list(reversed(produced)))

    async def test_unrelated_fibers_load_concurrently(self):
        gate = asyncio.Event()

        @plugin
        async def slow(_ctx, _config):
            await gate.wait()

        @plugin
        def fast(_ctx, _config):
            return None

        root = Context()
        slow_fiber = root.use(slow)
        fast_fiber = await root.use(fast)
        self.assertEqual(fast_fiber.state, FiberState.ACTIVE)
        self.assertEqual(slow_fiber.state, FiberState.LOADING)
        gate.set()
        await slow_fiber

    async def test_manual_outer_effect_disposes_nested_effects(self):
        seen = []
        order = []
        root = Context()

        def outer():
            root.on("nested", seen.append)
            return lambda: order.append("outer")

        dispose = await root.effect(outer)
        root.emit("nested", 1)
        await dispose()
        root.emit("nested", 2)
        self.assertEqual(seen, [1])
        self.assertEqual(order, ["outer"])

    async def test_cleanup_errors_are_reported_after_all_cleanup_runs(self):
        order = []

        def fail():
            order.append("fail")
            raise RuntimeError("cleanup failed")

        @plugin
        def sample(_ctx, _config):
            yield lambda: order.append("last")
            yield fail

        root = Context()
        fiber = await root.use(sample)
        with self.assertRaises(ExceptionGroup):
            await fiber.dispose()
        self.assertEqual(order, ["fail", "last"])
        self.assertTrue(root.errors)

    async def test_intercept_metadata_adapts_a_service(self):
        values = []

        @plugin(provide=["value"])
        def provider(ctx, _config):
            ctx.provide(
                "value",
                "cordis",
                adapt=lambda value, metadata, _ctx: metadata["prefix"] + value,
            )

        @plugin(inject={"value": {"prefix": "py-"}})
        def consumer(ctx, _config):
            values.append(ctx.value)

        root = Context()
        root.use(provider)
        root.use(consumer)
        await root.flush()
        self.assertEqual(values, ["py-cordis"])

    async def test_nested_intercepts_merge_with_nearest_layer_winning(self):
        root = Context()
        outer = root.intercept("value", {"outer": True, "shared": "outer"})
        inner = outer.intercept("value", {"inner": True, "shared": "inner"})

        self.assertEqual(
            inner.metadata("value"),
            {"outer": True, "inner": True, "shared": "inner"},
        )
        self.assertEqual(inner.intercept("value", "leaf").metadata("value"), "leaf")

    async def test_declared_provision_is_checked(self):
        @plugin(provide=["missing"])
        def incomplete(_ctx, _config):
            return None

        root = Context()
        with self.assertRaises(MissingProvisionError):
            await root.use(incomplete)

    async def test_parallel_aggregates_all_listener_errors(self):
        root = Context()
        root.on("event", lambda: (_ for _ in ()).throw(ValueError("one")))
        root.on("event", lambda: (_ for _ in ()).throw(TypeError("two")))
        with self.assertRaises(ExceptionGroup) as caught:
            await root.parallel("event")
        self.assertEqual(len(caught.exception.exceptions), 2)

    async def test_service_availability_predicate_is_reactive(self):
        available = False

        @plugin(provide=["value"])
        def provider(ctx, _config):
            ctx.provide("value", 1, check=lambda _ctx: available)

        @plugin(inject=["value"])
        def consumer(_ctx, _config):
            return None

        root = Context()
        root.use(provider)
        consumer_fiber = root.use(consumer)
        await root.flush()
        self.assertEqual(consumer_fiber.state, FiberState.PENDING)

        available = True
        root.notify("value")
        await root.flush()
        self.assertEqual(consumer_fiber.state, FiberState.ACTIVE)

        available = False
        root.notify("value")
        await root.flush()
        self.assertEqual(consumer_fiber.state, FiberState.PENDING)

    async def test_failed_config_update_restores_previous_instance(self):
        starts = []

        @plugin(validate=int)
        def configured(_ctx, config):
            starts.append(config)
            if config == 2:
                raise RuntimeError("bad config")

        root = Context()
        fiber = await root.use(configured, 1)
        with self.assertRaisesRegex(RuntimeError, "bad config"):
            await fiber.update(2)
        self.assertEqual(fiber.state, FiberState.ACTIVE)
        self.assertEqual(fiber.config, 1)
        self.assertEqual(starts, [1, 2, 1])


if __name__ == "__main__":
    unittest.main()
