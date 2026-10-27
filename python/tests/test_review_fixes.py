"""Behavioral regressions from the Python implementation review."""

import asyncio
import importlib
import sys
import tempfile
import unittest
from pathlib import Path

from cordis import Context, Entry, FiberState, Loader, ShutdownTimeoutError, plugin


@plugin(provide=["review.value"])
def provider(ctx, config):
    ctx.provide("review.value", config)


class LoaderReviewFixTests(unittest.IsolatedAsyncioTestCase):
    async def test_typed_consumer_failure_restores_provider_and_same_consumer(self):
        @plugin(inject=["review.value"], provide=["review.derived"])
        def consumer(ctx, config):
            value = ctx.service("review.value")
            if value == "new":
                raise ValueError("incompatible provider")
            ctx.provide("review.derived", (value, config))

        async with Context() as root:
            loader = Loader(root)
            original = [Entry("provider", provider, "old")]
            await loader.reconcile(original)
            mounted = await loader.mount("consumer", consumer.configure(42))
            with self.assertRaisesRegex(ValueError, "incompatible provider"):
                await loader.reconcile([Entry("provider", provider, "new")])
            self.assertEqual(loader.entries, tuple(original))
            self.assertIs(loader.get("consumer"), mounted)
            self.assertEqual(mounted.state, FiberState.ACTIVE)
            self.assertEqual(root.get("review.derived"), ("old", 42))

    async def test_newly_activated_typed_consumer_returns_to_pending(self):
        @plugin(inject=["review.value"])
        def consumer(ctx, config):
            raise ValueError("new consumer failed")

        async with Context() as root:
            loader = Loader(root)
            mounted = await loader.mount("consumer", consumer.configure(None))
            self.assertEqual(mounted.state, FiberState.PENDING)
            with self.assertRaisesRegex(ValueError, "new consumer failed"):
                await loader.reconcile([Entry("provider", provider, "new")])
            self.assertEqual(loader.entries, ())
            self.assertEqual(mounted.state, FiberState.PENDING)
            self.assertIsNone(mounted.error)

    async def test_existing_typed_failure_does_not_block_unrelated_reconcile(self):
        @plugin(inject=["other"])
        def broken(ctx, config):
            raise ValueError("existing failure")

        async with Context() as root:
            loader = Loader(root)
            mounted = await loader.mount("broken", broken.configure(None))
            root.provide("other", True)
            await root.flush()
            error = mounted.error
            self.assertIsInstance(error, ValueError)
            await loader.reconcile([Entry("provider", provider, "old")])
            self.assertIs(mounted.error, error)
            self.assertEqual(root.get("review.value"), "old")

    async def test_typed_consumer_recovery_failure_is_aggregated(self):
        attempts = []

        @plugin(inject=["review.value"])
        def consumer(ctx, config):
            value = ctx.service("review.value")
            attempts.append(value)
            if len(attempts) > 1:
                raise ValueError("consumer failed: " + value)

        async with Context() as root:
            loader = Loader(root)
            await loader.reconcile([Entry("provider", provider, "old")])
            mounted = await loader.mount("consumer", consumer.configure(None))
            with self.assertRaisesRegex(ExceptionGroup, "rollback failed") as caught:
                await loader.reconcile([Entry("provider", provider, "new")])
            self.assertEqual(attempts, ["old", "new", "old"])
            self.assertIn(mounted.error, caught.exception.exceptions)
            self.assertEqual(root.get("review.value"), "old")

    async def test_cancelled_reconcile_recovers_typed_transitive_consumer(self):
        entered, release = asyncio.Event(), asyncio.Event()
        self.addCleanup(release.set)

        @plugin(inject=["review.value"], provide=["review.middle"])
        def middle(ctx, config):
            ctx.provide("review.middle", ctx.service("review.value"))

        @plugin(inject=["review.middle"], provide=["review.result"])
        async def consumer(ctx, config):
            value = ctx.service("review.middle")
            if value == "new":
                entered.set()
                await release.wait()
                raise ValueError("incompatible indirect dependency")
            ctx.provide("review.result", value)

        async with Context() as root:
            loader = Loader(root)
            original = [Entry("provider", provider, "old")]
            await loader.reconcile(original)
            intermediate = await loader.mount("middle", middle.configure(None))
            mounted = await loader.mount("consumer", consumer.configure(None))
            updating = asyncio.create_task(
                loader.reconcile([Entry("provider", provider, "new")])
            )
            try:
                await entered.wait()
                updating.cancel()
            finally:
                release.set()
            with self.assertRaises(asyncio.CancelledError):
                await updating
            self.assertEqual(loader.entries, tuple(original))
            self.assertEqual(intermediate.state, FiberState.ACTIVE)
            self.assertEqual(mounted.state, FiberState.ACTIVE)
            self.assertEqual(root.get("review.result"), "old")


class HMRReviewFixTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)
        self.module = "cordis_review_dynamic_plugin"
        self.helper = "cordis_review_dynamic_helper"
        sys.path.insert(0, self.temp.name)
        self.addCleanup(sys.path.remove, self.temp.name)
        for name in (self.module, self.helper):
            self.addCleanup(sys.modules.pop, name, None)
        self.helper_path = self.path / (self.helper + ".py")
        self.helper_path.write_text("VALUE = 'old'\n", encoding="utf-8")
        self.plugin_path = self.path / (self.module + ".py")
        self.plugin_path.write_text(
            f"import importlib\nfrom cordis import plugin\n"
            f"helper = importlib.import_module({self.helper!r})\n"
            "@plugin(provide=['review.value'])\n"
            "def main(ctx, config):\n"
            "    ctx.provide('review.value', helper.VALUE)\n",
            encoding="utf-8",
        )
        importlib.invalidate_caches()

    async def test_identical_reconcile_preserves_dynamic_reload_dependencies(self):
        async with Context() as root:
            loader = Loader(root)
            entries = [Entry("plugin", self.module + ":main")]
            await loader.reconcile(entries)
            await loader.reconcile(entries)
            self.helper_path.write_text("VALUE = 'new-value'\n", encoding="utf-8")
            self.assertEqual(
                await loader.reload_changed([self.helper_path]), ("plugin",)
            )
            self.assertEqual(root.get("review.value"), "new-value")

    async def test_reexecuted_module_drops_removed_dynamic_dependency(self):
        async with Context() as root:
            loader = Loader(root)
            await loader.reconcile([Entry("plugin", self.module + ":main")])
            self.plugin_path.write_text(
                "from cordis import plugin\n"
                "@plugin(provide=['review.value'])\n"
                "def main(ctx, config):\n"
                "    ctx.provide('review.value', 'independent')\n",
                encoding="utf-8",
            )
            await loader.reload_changed([self.plugin_path])
            self.assertEqual(await loader.reload_changed([self.helper_path]), ())
            self.assertEqual(root.get("review.value"), "independent")


class OnceReviewFixTests(unittest.IsolatedAsyncioTestCase):
    async def test_async_once_uses_only_callback_task_capacity(self):
        seen = []

        async def callback():
            seen.append("called")

        async with Context(max_tasks=1) as root:
            handle = root.once("message", callback)
            await root.parallel("message")
            await root.parallel("message")
            await handle()
            self.assertEqual(seen, ["called"])
            self.assertEqual(root.errors, ())

    async def test_intercepted_cleanup_stays_owned_until_shutdown_finishes(self):
        entered, release, closed = asyncio.Event(), asyncio.Event(), asyncio.Event()
        callbacks = []
        root = Context(max_tasks=1)
        self.addAsyncCleanup(root.close)
        self.addCleanup(release.set)

        async def cleanup():
            entered.set()
            await release.wait()
            closed.set()

        def intercept(ctx, name, callback, options):
            if name == "external":
                callbacks.append(callback)
                return cleanup

        root.on("internal/listener", intercept)
        seen = []
        root.once("external", lambda: seen.append("called"))
        callbacks[0]()
        await entered.wait()
        self.assertEqual(seen, ["called"])
        self.assertEqual(root.task_count, 0)
        try:
            with self.assertRaises(ShutdownTimeoutError):
                await root.close(timeout=0)
            self.assertFalse(closed.is_set())
        finally:
            release.set()
        await root.close()
        self.assertTrue(closed.is_set())

    async def test_once_cleanup_failure_is_reported_to_owner(self):
        reported = asyncio.Event()
        root = Context(max_tasks=1)
        self.addAsyncCleanup(root.close)
        callbacks = []

        async def cleanup():
            raise ValueError("registration cleanup failed")

        def intercept(ctx, name, callback, options):
            if name == "event":
                callbacks.append(callback)
                return cleanup

        root.on("internal/listener", intercept)
        root.on("internal/error", lambda error, owner: reported.set())
        handle = root.once("event", lambda: None)
        callbacks[0]()
        await reported.wait()
        self.assertEqual(len(root.errors), 1)
        error, owner = root.errors[0]
        self.assertIs(owner, root.fiber)
        self.assertIsInstance(error, ExceptionGroup)
        self.assertEqual(str(error.exceptions[0]), "registration cleanup failed")
        with self.assertRaises(ExceptionGroup):
            await handle()
