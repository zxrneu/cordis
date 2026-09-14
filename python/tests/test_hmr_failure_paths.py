import asyncio
import importlib
import sys
import threading
import unittest
from unittest.mock import patch

from cordis import (
    Context,
    Entry,
    FiberState,
    FullRestartRequired,
    HotReloader,
    Loader,
    plugin,
)
from . import test_regressions


class HMRFailurePaths(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = test_regressions.HMRAudit.asyncSetUp

    async def test_consumer_failure_rolls_back_provider_and_recovers_consumers(self):
        seen = []

        @plugin(inject=["version"], provide=["derived"])
        def consumer(ctx, config):
            seen.append(ctx.version)
            if ctx.version == "v2":
                raise ValueError("consumer rejects v2")
            ctx.provide("derived", ctx.version)

        @plugin(inject=["derived"])
        def downstream(ctx, config):
            self.assertEqual(ctx.derived, "v1")

        async with Context() as root:
            loader = Loader(root)
            await loader.reconcile([
                Entry("app", f"{self.module}:app"),
                Entry("consumer", consumer),
                Entry("downstream", downstream),
            ])
            old_module = sys.modules[self.module]
            self.path.write_text(test_regressions.module_source("v2"), encoding="utf-8")
            with self.assertRaisesRegex(ExceptionGroup, "hot reload failed"):
                await loader.reload("app")
            self.assertEqual(root.get("version"), "v1")
            self.assertEqual(root.get("derived"), "v1")
            self.assertEqual(seen, ["v1", "v2", "v1"])
            self.assertIs(sys.modules[self.module], old_module)
            for entry_id in ("app", "consumer", "downstream"):
                self.assertEqual(loader.get(entry_id).state, FiberState.ACTIVE)

    async def test_transitive_consumer_outside_loader_is_recovered(self):
        @plugin(inject=["version"], provide=["derived"])
        def intermediate(ctx, config):
            ctx.provide("derived", ctx.version)

        @plugin(inject=["derived"])
        def consumer(ctx, config):
            if ctx.derived == "v2":
                raise ValueError("downstream rejects v2")

        async with Context() as root:
            loader = Loader(root)
            await loader.reconcile([Entry("app", f"{self.module}:app")])
            middle = await root.use(intermediate)
            last = await root.use(consumer)
            self.path.write_text(test_regressions.module_source("v2"), encoding="utf-8")
            with self.assertRaisesRegex(ExceptionGroup, "hot reload failed"):
                await loader.reload("app")
            self.assertEqual(root.get("version"), "v1")
            self.assertEqual(middle.state, FiberState.ACTIVE)
            self.assertEqual(last.state, FiberState.ACTIVE)

    async def test_unrelated_existing_failure_does_not_block_reload(self):
        def broken(ctx, config):
            raise ValueError("existing failure")

        async with Context() as root:
            failed = root.use(broken)
            with self.assertRaisesRegex(ValueError, "existing failure"):
                await failed
            error = failed.error
            loader = Loader(root)
            await loader.reconcile([Entry("app", f"{self.module}:app")])
            self.path.write_text(test_regressions.module_source("v2"), encoding="utf-8")
            await loader.reload("app")
            self.assertEqual(root.get("version"), "v2")
            self.assertIs(failed.error, error)
            self.assertEqual(failed.state, FiberState.FAILED)

    async def test_newly_available_consumer_failure_restores_pending_state(self):
        self.path.write_text(
            test_regressions.module_source("v1").replace(
                "ctx.provide('version', VERSION)",
                "ctx.provide('version', VERSION, check=lambda ctx: False)",
            ),
            encoding="utf-8",
        )

        @plugin(inject=["version"])
        def consumer(ctx, config):
            raise ValueError("newly activated consumer failed")

        async with Context() as root:
            loader = Loader(root)
            await loader.reconcile([Entry("app", f"{self.module}:app")])
            fiber = await root.use(consumer)
            self.assertEqual(fiber.state, FiberState.PENDING)
            self.path.write_text(test_regressions.module_source("v2"), encoding="utf-8")
            with self.assertRaisesRegex(ExceptionGroup, "hot reload failed"):
                await loader.reload("app")
            self.assertEqual(fiber.state, FiberState.PENDING)
            self.assertIsNone(fiber.error)

    async def test_consumer_recovery_failure_is_reported(self):
        attempts = []

        @plugin(inject=["version"])
        def consumer(ctx, config):
            attempts.append(ctx.version)
            if len(attempts) > 1:
                raise ValueError("consumer failed: " + ctx.version)

        async with Context() as root:
            loader = Loader(root)
            await loader.reconcile([Entry("app", f"{self.module}:app")])
            fiber = await root.use(consumer)
            self.path.write_text(test_regressions.module_source("v2"), encoding="utf-8")
            with self.assertRaisesRegex(
                ExceptionGroup, "hot reload and rollback both failed"
            ) as raised:
                await loader.reload("app")
            self.assertEqual(attempts, ["v1", "v2", "v1"])
            self.assertEqual(root.get("version"), "v1")
            self.assertIn(fiber.error, raised.exception.exceptions)
            self.assertEqual(str(fiber.error), "consumer failed: v1")

    async def asyncTearDown(self):
        for name in list(sys.modules):
            if name == self.package or name.startswith(self.package + "."):
                sys.modules.pop(name, None)
        sys.path.remove(self.temp.name)
        self.temp.cleanup()

    async def test_package_expansion_respects_external_child_boundary(self):
        helper = self.path.with_name("helper.py")
        helper.write_text("VERSION = 'v1'\n", encoding="utf-8")
        async with Context() as root:
            loader = Loader(root)
            await loader.reconcile([Entry("app", f"{self.module}:app")])
            old_helper = importlib.import_module(self.package + ".helper")
            with self.assertRaises(FullRestartRequired):
                await HotReloader(
                    loader, externals=[self.package + ".helper"]
                ).reload_changed([self.path.with_name("__init__.py")])
            self.assertIs(sys.modules[self.package + ".helper"], old_helper)
            self.assertEqual(root.get("version"), "v1")

    async def test_failed_import_removes_new_child_and_restores_nonmodule_attribute(
        self,
    ):
        async with Context() as root:
            loader = Loader(root)
            await loader.reconcile([Entry("app", f"{self.module}:app")])
            package = sys.modules[self.package]
            old_module = sys.modules[self.module]
            package.helper = "previous attribute"
            helper = self.path.with_name("helper.py")
            helper.write_text("VALUE = 'new helper'\n", encoding="utf-8")
            self.path.write_text(
                "import importlib\n"
                "importlib.import_module(__package__ + '.helper')\n"
                "raise RuntimeError('candidate import failed')\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(RuntimeError, "candidate import failed"):
                await loader.reload("app")
            self.assertIs(sys.modules[self.module], old_module)
            self.assertIs(package.app, old_module)
            self.assertNotIn(self.package + ".helper", sys.modules)
            self.assertEqual(package.helper, "previous attribute")
            self.assertEqual(root.get("version"), "v1")

    async def test_cancel_during_import_waits_for_thread_and_restores_modules(self):
        entered, release = threading.Event(), threading.Event()
        async with Context() as root:
            loader = Loader(root)
            await loader.reconcile([Entry("app", f"{self.module}:app")])
            old_module = sys.modules[self.module]
            previous = loader.get("app")

            def failed_import(entry):
                entered.set()
                release.wait(2)
                raise ImportError("late import failure")

            with patch.object(loader, "_component", side_effect=failed_import):
                task = asyncio.create_task(loader.reload("app"))
                try:
                    self.assertTrue(await asyncio.to_thread(entered.wait, 1))
                    task.cancel()
                    await asyncio.sleep(0)
                    self.assertFalse(task.done())
                finally:
                    release.set()
                with self.assertRaises(asyncio.CancelledError) as raised:
                    await task
                self.assertIsInstance(raised.exception.__cause__, ImportError)
            self.assertIs(sys.modules[self.module], old_module)
            self.assertIs(sys.modules[self.package].app, old_module)
            self.assertIs(loader.get("app"), previous)
            self.assertEqual(root.get("version"), "v1")

    async def test_cancel_during_old_cleanup_finishes_successful_replacement(self):
        entered, release = asyncio.Event(), asyncio.Event()
        self.path.write_text(
            "from cordis import plugin\n"
            "@plugin(provide=['version'])\n"
            "def app(ctx, config):\n"
            "    ctx.provide('version', 'v1')\n"
            "    async def cleanup():\n"
            "        config[0].set()\n"
            "        await config[1].wait()\n"
            "    return cleanup\n",
            encoding="utf-8",
        )
        async with Context() as root:
            loader = Loader(root)
            await loader.reconcile(
                [Entry("app", f"{self.module}:app", (entered, release))]
            )
            self.path.write_text(test_regressions.module_source("v2"), encoding="utf-8")
            task = asyncio.create_task(loader.reload("app"))
            await entered.wait()
            task.cancel()
            await asyncio.sleep(0)
            self.assertFalse(task.done())
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertEqual(root.get("version"), "v2")
            self.assertEqual(loader.get("app").state, FiberState.ACTIVE)
            self.assertIs(sys.modules[self.package].app, sys.modules[self.module])

    async def test_package_reload_reimports_cached_children(self):
        helper = self.path.with_name("helper.py")
        helper.write_text("VERSION = 'v1'\n", encoding="utf-8")
        self.path.write_text(
            "from cordis import plugin\n"
            "from . import helper\n"
            "@plugin(provide=['version'])\n"
            "def app(ctx, config):\n"
            "    ctx.provide('version', helper.VERSION)\n",
            encoding="utf-8",
        )
        importlib.invalidate_caches()
        async with Context() as root:
            loader = Loader(root)
            await loader.reconcile([Entry("app", f"{self.module}:app")])
            old_helper = sys.modules[self.package + ".helper"]
            helper.write_text("VERSION = 'v2'\n", encoding="utf-8")
            await HotReloader(loader).reload_entries(["app"], accepted={self.package})
            self.assertEqual(root.get("version"), "v2")
            self.assertIsNot(sys.modules[self.package + ".helper"], old_helper)
            self.assertIs(
                sys.modules[self.package].helper, sys.modules[self.package + ".helper"]
            )
