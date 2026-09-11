import asyncio
import unittest
from dataclasses import dataclass

from cordis import (
    Context,
    DuplicateQueryError,
    Entry,
    EventKey,
    FiberState,
    Loader,
    QueryKey,
    ServiceKey,
    plugin,
)


VALUE = ServiceKey[int]("value")


class AppContext(Context):
    value = VALUE


@dataclass(frozen=True)
class WorkerConfig:
    value: int


@plugin(provide=[VALUE])
def worker(ctx: AppContext, config: WorkerConfig) -> None:
    ctx.provide(VALUE, config.value)


def decode_worker(raw: object) -> WorkerConfig:
    if not isinstance(raw, dict) or not isinstance(raw.get("value"), int):
        raise ValueError("expected an integer value")
    return WorkerConfig(raw["value"])


class BoundMountTests(unittest.IsolatedAsyncioTestCase):
    async def test_mount_keeps_context_config_and_lifecycle_identity(self):
        async with AppContext() as root:
            loader = Loader(root)
            binding = worker.configure(WorkerConfig(1))
            fiber = await loader.mount("worker", binding)
            self.assertIsInstance(fiber.ctx, AppContext)
            self.assertIs(fiber.ctx.fiber, fiber)
            self.assertIs(loader.get("worker"), fiber)
            self.assertIs(await loader.get("worker").wait(), fiber)
            self.assertIs(await loader.get("worker"), fiber)
            self.assertEqual(root.value, 1)

            await fiber.update(WorkerConfig(2))
            self.assertEqual(root.value, 2)
            self.assertIs(await loader.reload("worker"), fiber)
            self.assertEqual(root.value, 2)
            self.assertEqual(binding.config, WorkerConfig(1))
            await loader.unmount("worker")
            self.assertEqual(fiber.state, FiberState.DISPOSED)
            self.assertIsNone(root.get(VALUE))
            with self.assertRaises(KeyError):
                loader.get("worker")

    async def test_typed_mount_and_declarative_entries_have_separate_ownership(self):
        seen = []

        @plugin
        def legacy(ctx, config):
            seen.append(config)
            return lambda: seen.append(-config)

        async with AppContext() as root:
            loader = Loader(root)
            mounted = await loader.mount("worker", worker.configure(WorkerConfig(1)))
            await loader.reconcile([Entry("legacy", legacy, 3)])
            await loader.reconcile([])
            self.assertEqual(seen, [3, -3])
            self.assertEqual(mounted.state, FiberState.ACTIVE)

            with self.assertRaises(ValueError):
                await loader.mount("worker", worker.configure(WorkerConfig(2)))
            with self.assertRaises(ValueError):
                await loader.reconcile([Entry("worker", legacy, 4)])
            self.assertEqual(root.value, 1)

            await loader.reconcile([Entry("legacy", legacy, 5)])
            with self.assertRaises(ValueError):
                await loader.mount("legacy", worker.configure(WorkerConfig(2)))
            await loader.close()
            self.assertEqual(mounted.state, FiberState.DISPOSED)
            self.assertEqual(seen, [3, -3, 5, -5])

    async def test_decode_is_separate_from_activation_validation(self):
        validations = []

        def validate(config: WorkerConfig) -> WorkerConfig:
            validations.append(config.value)
            return config

        @plugin(provide=[VALUE], validate=validate)
        def validated(ctx: AppContext, config: WorkerConfig) -> None:
            ctx.provide(VALUE, config.value)

        async def async_decode(raw: object) -> WorkerConfig:
            await asyncio.sleep(0)
            return decode_worker(raw)

        async with AppContext() as root:
            loader = Loader(root)
            binding = await validated.decode({"value": 7}, decode_worker)
            self.assertEqual(validations, [])
            await loader.mount("worker", binding)
            self.assertEqual(validations, [7])
            with self.assertRaises(ValueError):
                await validated.decode({"value": "bad"}, decode_worker)
            self.assertEqual(root.value, 7)
            self.assertEqual(validations, [7])
            await loader.unmount("worker")
            binding = await validated.decode({"value": 8}, async_decode)
            await loader.mount("worker", binding)
            self.assertEqual(root.value, 8)
            self.assertEqual(validations, [7, 8])

    async def test_failed_mount_cleans_up_and_releases_its_id(self):
        seen = []

        @plugin
        def broken(ctx: AppContext, config: WorkerConfig):
            ctx.provide(VALUE, config.value)
            yield lambda: seen.append("cleaned")
            raise ValueError("startup failed")

        async with AppContext() as root:
            loader = Loader(root)
            with self.assertRaisesRegex(ValueError, "startup failed"):
                await loader.mount("worker", broken.configure(WorkerConfig(3)))
            self.assertEqual(seen, ["cleaned"])
            self.assertIsNone(root.get(VALUE))
            with self.assertRaises(KeyError):
                loader.get("worker")
            await loader.mount("worker", worker.configure(WorkerConfig(4)))
            self.assertEqual(root.value, 4)

    async def test_mount_preserves_dependency_waiting(self):
        seen = []

        @plugin(inject=[VALUE])
        def consumer(ctx: AppContext, config: None) -> None:
            seen.append(ctx.value)

        async with AppContext() as root:
            loader = Loader(root)
            fiber = await loader.mount("consumer", consumer.configure(None))
            self.assertEqual(fiber.state, FiberState.PENDING)
            await loader.mount("worker", worker.configure(WorkerConfig(9)))
            await root.flush()
            self.assertEqual(seen, [9])

    async def test_root_close_owns_typed_mounts(self):
        root = AppContext()
        loader = Loader(root)
        fiber = await loader.mount("worker", worker.configure(WorkerConfig(1)))
        await root.close()
        self.assertEqual(fiber.state, FiberState.DISPOSED)
        self.assertIsNone(root.get(VALUE))
        await loader.close()


class TypedEventTests(unittest.IsolatedAsyncioTestCase):
    async def test_notification_keys_are_distinct_from_names_and_other_keys(self):
        key = EventKey[str]("message")
        same_name = EventKey[int]("message")
        seen = []
        async with Context() as root:
            root.on(key, lambda message: seen.append(("typed", message)))
            root.on(same_name, lambda message: seen.append(("other", message)))
            root.on("message", lambda *args: seen.append(("legacy", args)))
            root.emit(key, "hello")
            root.emit(same_name, 3)
            root.emit("message", 1, 2)
            self.assertEqual(seen, [("typed", "hello"), ("other", 3), ("legacy", (1, 2))])

    async def test_typed_once_async_parallel_and_scopes(self):
        key = EventKey[int]("number")
        seen = []
        async with Context() as root:
            left = root.isolate("value")
            right = root.isolate("value")

            async def receive(value: int) -> None:
                await asyncio.sleep(0)
                seen.append(("left", value))

            left.once(key, receive)
            right.on(key, lambda value: seen.append(("right", value)))
            await root.parallel(key, 1, scope=left)
            await root.parallel(key, 2, scope=left)
            await root.parallel(key, 3, scope=right)
            self.assertEqual(seen, [("left", 1), ("right", 3)])

    async def test_query_false_none_and_single_handler_contract(self):
        key = QueryKey[int, bool]("allowed")
        async with Context() as root:
            self.assertIsNone(await root.query(key, 1))
            remove = root.handle(key, lambda user_id: False)
            self.assertIs(await root.query(key, 1), False)
            with self.assertRaises(DuplicateQueryError):
                root.handle(key, lambda user_id: True)
            await remove()
            root.handle(key, lambda user_id: None)
            self.assertIsNone(await root.query(key, 1))
            other = QueryKey[int, str]("allowed")
            root.handle(other, lambda user_id: "separate")
            self.assertEqual(await root.query(other, 1), "separate")

    async def test_query_runs_in_handler_scope_and_drains_before_cleanup(self):
        key = QueryKey[int, str]("read")
        entered, release = asyncio.Event(), asyncio.Event()
        seen = []

        @plugin
        def provider(ctx: Context, config: None):
            async def read(request: int) -> str:
                entered.set()
                await release.wait()
                seen.append("finished")
                return str(request)

            ctx.handle(key, read)
            return lambda: seen.append("closed")

        async with Context() as root:
            fiber = await root.use(provider)
            query = asyncio.create_task(root.query(key, 7))
            await entered.wait()
            self.assertEqual(len(fiber.tasks), 1)
            disposal = asyncio.create_task(fiber.dispose())
            await asyncio.sleep(0)
            try:
                self.assertFalse(disposal.done())
                self.assertIsNone(await root.query(key, 8))
            finally:
                release.set()
            self.assertEqual(await query, "7")
            await disposal
            self.assertEqual(seen, ["finished", "closed"])
            self.assertIsNone(await root.query(key, 9))

    async def test_queries_and_notifications_follow_nested_effect_lifetime(self):
        query_key = QueryKey[int, int]("double")
        event_key = EventKey[int]("number")
        seen = []
        async with Context() as root:
            def install():
                root.handle(query_key, lambda request: request * 2)
                root.on(event_key, seen.append)

            remove = await root.effect(install)
            self.assertEqual(await root.query(query_key, 3), 6)
            root.emit(event_key, 3)
            await remove()
            root.emit(event_key, 4)
            self.assertEqual(seen, [3])
            self.assertIsNone(await root.query(query_key, 4))

    async def test_query_async_errors_propagate_and_have_handler_ownership(self):
        key = QueryKey[int, str]("broken")

        @plugin
        def provider(ctx: Context, config: None) -> None:
            async def broken(request: int) -> str:
                raise ValueError("query failed")
            ctx.handle(key, broken)

        async with Context() as root:
            fiber = await root.use(provider)
            with self.assertRaisesRegex(ValueError, "query failed"):
                await root.query(key, 1)
            self.assertEqual(len(root.errors), 1)
            self.assertIs(root.errors[0][1], fiber)

    def test_keys_require_nonempty_names(self):
        with self.assertRaises(ValueError):
            EventKey[int]("")
        with self.assertRaises(ValueError):
            QueryKey[int, str]("")
