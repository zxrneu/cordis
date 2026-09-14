import asyncio
import importlib
import sys
import threading
import unittest
from unittest.mock import patch

from cordis import Context, Entry, FiberState, FullRestartRequired, HotReloader, Loader
from . import test_regressions


class HMRFailurePaths(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = test_regressions.HMRAudit.asyncSetUp

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
