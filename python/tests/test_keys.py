import unittest
from dataclasses import dataclass

from cordis import (
    Component,
    Context,
    DuplicateServiceError,
    Entry,
    FiberState,
    Loader,
    ServiceKey,
    UndeclaredServiceError,
    plugin,
)


COUNT = ServiceKey[int]("count")


class AppContext(Context):
    count = COUNT


@dataclass(frozen=True)
class CounterConfig:
    value: int


@plugin(provide=[COUNT])
def counter(ctx: AppContext, config: CounterConfig) -> None:
    ctx.provide(COUNT, config.value)


class KeyTests(unittest.IsolatedAsyncioTestCase):
    async def test_typed_key_reads_writes_and_string_interoperability(self):
        async with Context() as ctx:
            self.assertIsNone(ctx.get(COUNT))
            self.assertEqual(ctx.get(COUNT, 42), 42)
            await ctx.provide(COUNT, 1)()  # Explicit removal uses the same realm.
            ctx.provide(COUNT, 2)
            self.assertEqual(ctx[COUNT], 2)
            self.assertEqual(ctx.service(COUNT), 2)
            self.assertEqual(ctx.service("count"), 2)
            ctx.set(COUNT, 3)
            self.assertEqual(ctx.get(COUNT), 3)
            with self.assertRaises(DuplicateServiceError):
                ctx.provide("count", 4)

    async def test_descriptor_and_config_types_survive_reload(self):
        seen = []

        @plugin(inject=[COUNT])
        def consumer(ctx: AppContext, _config: None):
            self.assertIsInstance(ctx, AppContext)
            seen.append(ctx.count)
            return lambda: seen.append(-ctx.count)

        async with AppContext() as ctx:
            consumer_fiber = ctx.use(consumer)
            await ctx.flush()
            self.assertEqual(consumer_fiber.state, FiberState.PENDING)
            provider = await ctx.use(counter, CounterConfig(1))
            await provider.update(CounterConfig(2))
            self.assertEqual(provider.config, CounterConfig(2))
            await provider.dispose()
            self.assertEqual(seen, [1, -1, 2, -2])
            self.assertEqual(consumer_fiber.state, FiberState.PENDING)
            self.assertIs(AppContext.count, COUNT)

    async def test_descriptors_do_not_bypass_dependency_declarations(self):
        @plugin
        def undeclared(ctx: AppContext, _config: None):
            _ = ctx.count

        async with AppContext() as ctx:
            await ctx.use(counter, CounterConfig(1))
            with self.assertRaises(UndeclaredServiceError):
                await ctx.use(undeclared)

    async def test_isolation_preserves_subclass_and_service_key(self):
        async with AppContext() as ctx:
            left = ctx.isolate(COUNT)
            right = ctx.isolate(COUNT)
            self.assertIsInstance(left, AppContext)
            self.assertIsInstance(right, AppContext)
            left.provide(COUNT, 1)
            right.provide(COUNT, 2)
            self.assertEqual(left.count, 1)
            self.assertEqual(right.count, 2)
            self.assertIsNone(ctx.get(COUNT))

    async def test_interception_accepts_typed_dependency_keys(self):
        seen = []

        @plugin(inject={COUNT: {"increment": 2}})
        def consumer(ctx: AppContext, _config: None):
            seen.append(ctx.count)

        async with AppContext() as ctx:
            ctx.provide(
                COUNT,
                1,
                adapt=lambda value, metadata, _ctx: value + metadata["increment"],
            )
            await ctx.use(consumer)
            self.assertEqual(seen, [3])
            view = ctx.intercept(COUNT, {"increment": 4})
            self.assertIsInstance(view, AppContext)
            self.assertEqual(view.count, 5)
            self.assertEqual(view.metadata(COUNT), {"increment": 4})

    async def test_loader_accepts_typed_components_and_dataclass_configs(self):
        async with AppContext() as ctx:
            loader = Loader(ctx)
            await loader.reconcile([Entry("counter", counter, CounterConfig(7))])
            self.assertEqual(ctx.count, 7)
            await loader.reconcile([Entry("counter", counter, CounterConfig(8))])
            self.assertEqual(ctx.count, 8)
            await loader.close()

    async def test_adapter_uses_current_value_and_reading_context(self):
        seen = []

        def adapt(value: int, metadata: object, consumer: Context) -> int:
            assert isinstance(metadata, int)
            seen.append((value, metadata, consumer))
            return value + metadata

        async with Context() as ctx:
            ctx.provide(COUNT, 1, adapt=adapt)
            view = ctx.intercept(COUNT, 3)
            self.assertEqual(view[COUNT], 4)
            ctx.set(COUNT, 5)
            self.assertEqual(view[COUNT], 8)
            self.assertEqual(seen, [(1, 3, view), (5, 3, view)])

            # String services retain their dynamic result type, including an
            # adapter that returns a different type from the stored value.
            ctx.provide("label", 7, adapt=lambda value, _metadata, _ctx: str(value))
            self.assertEqual(ctx.get("label"), "7")

    async def test_generic_component_can_be_constructed_explicitly(self):
        component = Component[CounterConfig, AppContext](counter.apply, provide=[COUNT])
        async with AppContext() as ctx:
            await ctx.use(component, CounterConfig(9))
            self.assertEqual(ctx.count, 9)

    async def test_class_and_factory_plugin_lifecycle_protocols(self):
        seen = []

        class Lifecycle:
            def __init__(self, ctx: Context, config: CounterConfig) -> None:
                self.value = config.value

            async def start(self) -> None:
                seen.append(self.value)
                if self.value == 2:
                    raise ValueError("startup failed")

            async def stop(self) -> None:
                seen.append(-self.value)

        @plugin
        def factory(ctx: Context, config: CounterConfig) -> Lifecycle:
            return Lifecycle(ctx, config)

        async with Context() as ctx:
            fiber = await ctx.use(plugin(Lifecycle), CounterConfig(1))
            await fiber.dispose()
            with self.assertRaisesRegex(ValueError, "startup failed"):
                await ctx.use(factory, CounterConfig(2))
            self.assertEqual(seen, [1, -1, 2, -2])

    async def test_service_descriptors_cannot_be_shadowed(self):
        async with AppContext() as ctx:
            ctx.provide(COUNT, 1)
            with self.assertRaises(AttributeError):
                ctx.count = 2
            self.assertEqual(ctx.count, 1)

    def test_service_key_requires_a_name(self):
        with self.assertRaises(ValueError):
            ServiceKey[int]("")
