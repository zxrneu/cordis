import asyncio
import threading
import unittest
from unittest.mock import patch

from cordis import (
    Context,
    Entry,
    FiberState,
    InactiveContextError,
    InactiveServiceError,
    Loader,
    plugin,
)


def leaves(error):
    if isinstance(error, BaseExceptionGroup):
        return [item for child in error.exceptions for item in leaves(child)]
    return [error]


class CoreFailurePaths(unittest.IsolatedAsyncioTestCase):
    async def test_cancelled_direct_update_restores_old_config_after_failure(self):
        entered, release = asyncio.Event(), asyncio.Event()

        @plugin(provide=["value"])
        async def provider(ctx, config):
            if config == 2:
                entered.set()
                await release.wait()
                raise ValueError("bad config")
            ctx.provide("value", config)

        async with Context() as root:
            fiber = await root.use(provider, 1)
            update = asyncio.create_task(fiber.update(2))
            await entered.wait()
            update.cancel()
            await asyncio.sleep(0)
            self.assertFalse(update.done())
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await update
            self.assertEqual((fiber.config, root.get("value")), (1, 1))
            await fiber.dispose()
            with self.assertRaises(InactiveContextError):
                await fiber.update(3)
            self.assertEqual(fiber.config, 1)

    async def test_realm_change_drains_old_task_and_preserves_explicit_isolation(self):
        for new_provider in (False, True):
            with self.subTest(new_provider=new_provider):
                entered, release = asyncio.Event(), asyncio.Event()
                seen = []

                @plugin(provide=["db"])
                def provider(ctx, config):
                    ctx.provide("db", config)

                @plugin(inject=["db"])
                def consumer(ctx, config):
                    seen.append(("start", ctx.db))

                    async def work():
                        entered.set()
                        await release.wait()
                        seen.append(("work", ctx.db))
                        with self.assertRaises(InactiveServiceError):
                            ctx.isolate("db").service("db")

                    ctx.on("work", work)
                    return lambda: seen.append(("stop", ctx.db))

                async with Context() as root:
                    loader = Loader(root)
                    providers = [Entry("a", provider, "old", isolate={"db": "a"})]
                    if new_provider:
                        providers.append(
                            Entry("b", provider, "new", isolate={"db": "b"})
                        )
                    await loader.reconcile(
                        [*providers, Entry("c", consumer, isolate={"db": "a"})]
                    )
                    job = asyncio.create_task(root.parallel("work"))
                    await entered.wait()
                    moving = asyncio.create_task(
                        loader.reconcile(
                            [
                                *providers,
                                Entry("c", consumer, isolate={"db": "b"}),
                            ]
                        )
                    )
                    async with asyncio.timeout(2):
                        while loader.get("c").state is not FiberState.UNLOADING:
                            await asyncio.sleep(0)
                        release.set()
                        await asyncio.gather(job, moving)
                    expected = [("start", "old"), ("work", "old"), ("stop", "old")]
                    if new_provider:
                        expected.append(("start", "new"))
                    self.assertEqual(seen, expected)
                    self.assertEqual(root.errors, ())

    async def test_async_validation_interrupted_by_dependency_loss_is_not_failure(self):
        entered, release = asyncio.Event(), asyncio.Event()

        async def validate(config):
            entered.set()
            await release.wait()
            return config

        @plugin(provide=["db"])
        def provider(ctx, config):
            ctx.provide("db", "db")

        @plugin(inject=["db"], provide=["derived"], validate=validate)
        def consumer(ctx, config):
            ctx.provide("derived", ctx.db)

        async with Context() as root:
            provider_fiber = await root.use(provider)
            consumer_fiber = root.use(consumer)
            await entered.wait()
            disposal = asyncio.create_task(provider_fiber.dispose())
            await asyncio.sleep(0)
            release.set()
            await disposal
            self.assertEqual(consumer_fiber.state, FiberState.PENDING)
            self.assertIsNone(consumer_fiber.error)
            await root.use(provider)
            await root.flush()
            self.assertEqual(root.get("derived"), "db")

    async def test_generator_failure_preserves_close_error_and_rolls_back(self):
        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                seen = []

                def initialize():
                    try:
                        yield lambda: seen.append("undo")
                        yield 123
                    finally:
                        seen.append("closed")
                        raise ValueError("close failed")

                async def initialize_async():
                    try:
                        yield lambda: seen.append("undo")
                        yield 123
                    finally:
                        await asyncio.sleep(0)
                        seen.append("closed")
                        raise ValueError("close failed")

                iterator = initialize_async() if asynchronous else initialize()
                async with Context() as root:
                    with self.assertRaises(ExceptionGroup) as raised:
                        await root.effect(lambda: iterator)
                    errors = leaves(raised.exception)
                    self.assertTrue(
                        any(isinstance(error, TypeError) for error in errors)
                    )
                    self.assertTrue(
                        any(isinstance(error, ValueError) for error in errors)
                    )
                    self.assertEqual(seen, ["closed", "undo"])
                    await root.effect(lambda: root.provide("after", "valid"))
                    self.assertEqual(root.service("after"), "valid")

    async def test_nested_service_handle_guard_runs_before_mutation(self):
        jobs = []
        async with Context() as root:
            remove_outer = await root.effect(lambda: root.provide("db", "old"))

            @plugin(inject=["db"])
            def consumer(ctx, config):
                async def job():
                    with self.assertRaises(InactiveContextError):
                        async with asyncio.timeout(1):
                            await remove_outer()
                    self.assertEqual(ctx.db, "old")

                jobs.append(ctx.spawn(job()))

            fiber = await root.use(consumer)
            await asyncio.gather(*jobs)
            self.assertEqual(fiber.state, FiberState.ACTIVE)
            await remove_outer()
            self.assertEqual(fiber.state, FiberState.PENDING)

    async def test_unrelated_managed_task_can_remove_unused_service(self):
        async with Context() as root:
            remove = root.provide("unused", 1)
            await root.spawn(remove())
            self.assertIsNone(root.get("unused"))


class LoaderFailurePaths(unittest.IsolatedAsyncioTestCase):
    async def test_unchanged_consumer_failure_rolls_back_changed_provider(self):
        @plugin(provide=["value"])
        def provider(ctx, config):
            ctx.provide("value", config)

        @plugin(inject=["value"], provide=["derived"])
        def consumer(ctx, config):
            if ctx.value == "new":
                raise ValueError("incompatible provider")
            ctx.provide("derived", ctx.value)

        async with Context() as root:
            loader = Loader(root)
            original = [Entry("p", provider, "old"), Entry("c", consumer)]
            await loader.reconcile(original)
            with self.assertRaisesRegex(ValueError, "incompatible provider"):
                await loader.reconcile([Entry("p", provider, "new"), original[1]])
            self.assertEqual(loader.entries, tuple(original))
            self.assertEqual(root.get("derived"), "old")
            self.assertEqual(loader.get("c").state, FiberState.ACTIVE)

    async def test_preflight_failures_do_not_stop_existing_fiber(self):
        @plugin(provide=["value"])
        def good(ctx, config):
            ctx.provide("value", config)

        def invalid(config):
            raise ValueError("invalid config")

        bad = plugin(lambda ctx, config: None, validate=invalid)
        async with Context() as root:
            loader = Loader(root)
            await loader.reconcile([Entry("old", good, 1)])
            previous = loader.get("old")
            for entries in (
                [Entry("new", bad, 2)],
                [{"id": "new", "plugin": good, "isolate": {"value": 123}}],
                [Entry("group", children=(Entry("nested", "invalid-locator"),))],
                [Entry("group", children=(Entry("same", good), Entry("same", good)))],
            ):
                with self.subTest(entries=entries):
                    with self.assertRaises((TypeError, ValueError)):
                        await loader.reconcile(entries)
                    self.assertIs(loader.get("old"), previous)
                    self.assertEqual(root.get("value"), 1)

    async def test_failed_activation_restores_removed_entries_and_prior_updates(self):
        @plugin
        def provider(ctx, config):
            name, value = config
            if value == "bad":
                raise ValueError("activation failed")
            ctx.provide(name, value)

        async with Context() as root:
            loader = Loader(root)
            original = [
                Entry(key, provider, (key, "old"))
                for key in ("keep", "update", "remove")
            ]
            await loader.reconcile(original)
            untouched = loader.get("keep")
            for failed in (
                Entry("new", provider, ("new", "bad")),
                Entry("group", children=(Entry("bad", provider, ("new", "bad")),)),
            ):
                with self.subTest(failed=failed):
                    with self.assertRaisesRegex(ValueError, "activation failed"):
                        await loader.reconcile(
                            [
                                original[0],
                                Entry("update", provider, ("update", "new")),
                                failed,
                            ]
                        )
                    self.assertEqual(loader.entries, tuple(original))
                    self.assertIs(loader.get("keep"), untouched)
                    self.assertEqual(
                        [root.get(key) for key in ("keep", "update", "remove")],
                        ["old"] * 3,
                    )
                    self.assertIsNone(root.get("new"))

    async def test_cancelled_update_hook_restores_prior_entry_changes(self):
        async def update(ctx, previous, config):
            raise asyncio.CancelledError("hook cancelled")

        @plugin(update=update)
        def app(ctx, config):
            ctx.provide("value", config)

        @plugin
        def other(ctx, config):
            ctx.provide("other", config)

        async with Context() as root:
            loader = Loader(root)
            original = [Entry("other", other, 1), Entry("app", app, 1)]
            await loader.reconcile(original)
            with self.assertRaises(asyncio.CancelledError):
                await loader.reconcile([Entry("app", app, 2)])
            self.assertEqual(loader.entries, tuple(original))
            self.assertEqual(root.get("other"), 1)
            self.assertEqual(root.get("value"), 1)

    async def test_concurrent_reconcile_is_serial_and_cancelled_waiter_does_not_mutate(
        self,
    ):
        entered, release = asyncio.Event(), asyncio.Event()
        seen = []

        @plugin(provide=["value"])
        async def provider(ctx, config):
            seen.append(config)
            if config == 2:
                entered.set()
                await release.wait()
            ctx.provide("value", config)

        async with Context() as root:
            loader = Loader(root)
            await loader.reconcile([Entry("app", provider, 1)])
            first = asyncio.create_task(loader.reconcile([Entry("app", provider, 2)]))
            await entered.wait()
            cancelled = asyncio.create_task(
                loader.reconcile([Entry("app", provider, 99)])
            )
            second = asyncio.create_task(loader.reconcile([Entry("app", provider, 3)]))
            await asyncio.sleep(0)
            cancelled.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await cancelled
            self.assertEqual(seen, [1, 2])
            release.set()
            await asyncio.gather(first, second)
            self.assertEqual(seen, [1, 2, 3])
            self.assertEqual(root.get("value"), 3)

    async def test_repeated_cancel_waits_for_failed_mutation_rollback(self):
        entered, release = asyncio.Event(), asyncio.Event()

        @plugin(provide=["value"])
        async def provider(ctx, config):
            if config == 2:
                entered.set()
                await release.wait()
                raise ValueError("candidate failed")
            ctx.provide("value", config)

        async with Context() as root:
            loader = Loader(root)
            await loader.reconcile([Entry("app", provider, 1)])
            changing = asyncio.create_task(
                loader.reconcile([Entry("app", provider, 2)])
            )
            await entered.wait()
            changing.cancel()
            await asyncio.sleep(0)
            changing.cancel()
            await asyncio.sleep(0)
            self.assertFalse(changing.done())
            release.set()
            with self.assertRaises(asyncio.CancelledError) as raised:
                await changing
            self.assertIsInstance(raised.exception.__cause__, ValueError)
            self.assertEqual(root.get("value"), 1)
            self.assertEqual(loader.entries[0].config, 1)
            await loader.reconcile([Entry("app", provider, 3)])
            self.assertEqual(root.get("value"), 3)

    async def test_managed_self_unload_is_rejected_before_shielding(self):
        jobs = []
        release = asyncio.Event()
        async with Context() as root:
            loader = Loader(root)

            @plugin
            def app(ctx, config):
                async def job():
                    await release.wait()
                    with self.assertRaises(InactiveContextError):
                        async with asyncio.timeout(1):
                            await loader.reconcile([])

                jobs.append(ctx.spawn(job()))

            await loader.reconcile([Entry("app", app)])
            release.set()
            await asyncio.gather(*jobs)
            self.assertEqual(loader.get("app").state, FiberState.ACTIVE)

    async def test_recursive_loader_mutation_is_rejected_without_lock_deadlock(self):
        async with Context() as root:
            loader = Loader(root)

            @plugin
            async def recursive(ctx, config):
                await loader.reconcile([])

            with self.assertRaisesRegex(InactiveContextError, "recursively"):
                await loader.reconcile([Entry("recursive", recursive)])
            self.assertEqual(loader.entries, ())

    async def test_failed_parallel_imports_finish_before_operation_returns(self):
        entered, release = threading.Event(), threading.Event()
        finished = []
        async with Context() as root:
            loader = Loader(root)
            original = loader._component

            def resolve(entry):
                if entry.id == "slow":
                    entered.set()
                    release.wait(2)
                    finished.append("slow")
                    return original(entry)
                raise ImportError("bad candidate")

            with patch.object(loader, "_component", side_effect=resolve):
                task = asyncio.create_task(
                    loader.reconcile(
                        [
                            Entry("bad", lambda ctx, config: None),
                            Entry("slow", lambda ctx, config: None),
                        ]
                    )
                )
                try:
                    self.assertTrue(await asyncio.to_thread(entered.wait, 1))
                    self.assertFalse(task.done())
                finally:
                    release.set()
                with self.assertRaises(ImportError):
                    await task
                self.assertEqual(finished, ["slow"])
