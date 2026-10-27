"""Regression coverage for B01–B08 and K01–K02 from the 2026-09-10 audit."""

import asyncio
import gc
import importlib
import sys
import tempfile
import unittest
import weakref
from pathlib import Path

from cordis import Context, Entry, FiberState, InactiveContextError, Loader, plugin


class CoreAudit(unittest.IsolatedAsyncioTestCase):
    async def test_b01_consumer_realm_change_preserves_cleanup_view(self):
        root = Context()
        loader = Loader(root)
        seen = []

        @plugin(provide=["db"])
        def provider(ctx, _config):
            ctx.provide("db", "old")

        @plugin(inject=["db"])
        def consumer(ctx, _config):
            def cleanup():
                try:
                    seen.append(ctx.db)
                except Exception as error:
                    # Retain the actual error without making fixture teardown fail.
                    seen.append(type(error).__name__)

            return cleanup

        async with root:
            await loader.reconcile(
                [
                    Entry("p", provider, isolate={"db": "a"}),
                    Entry("c", consumer, isolate={"db": "a"}),
                ]
            )
            await loader.reconcile(
                [
                    Entry("p", provider, isolate={"db": "a"}),
                    Entry("c", consumer, isolate={"db": "b"}),
                ]
            )
        self.assertEqual(seen, ["old"])

    async def test_b02_dispose_during_failed_start_removes_fiber(self):
        entered, release = asyncio.Event(), asyncio.Event()

        @plugin
        async def app(ctx, _config):
            entered.set()
            await release.wait()
            raise ValueError("startup failed")

        async with Context() as root:
            fiber = root.use(app)
            await entered.wait()
            disposal = asyncio.create_task(fiber.dispose())
            await asyncio.sleep(0)  # dispose sets desired=False before startup resumes.
            release.set()
            await disposal
            actual = (fiber.state, fiber.uid, fiber in root.fibers)
        self.assertEqual(actual, (FiberState.DISPOSED, None, False))

    async def test_b03_interrupted_generator_can_reactivate(self):
        entered, release = asyncio.Event(), asyncio.Event()

        @plugin(provide=["db"])
        def database(ctx, _config):
            ctx.provide("db", "value")

        @plugin(inject=["db"], provide=["derived"])
        async def consumer(ctx, _config):
            entered.set()
            await release.wait()
            yield lambda: None
            ctx.provide("derived", ctx.db)

        async with Context() as root:
            provider = await root.use(database)
            fiber = root.use(consumer)
            await entered.wait()
            disposal = asyncio.create_task(provider.dispose())
            await asyncio.sleep(0)  # provider_leaving changes the consumer target.
            release.set()
            await disposal
            await root.use(database)
            await root.flush()
            actual = (fiber.state, type(fiber.error).__name__, root.get("derived"))
        self.assertEqual(actual, (FiberState.ACTIVE, "NoneType", "value"))

    async def test_b06_disposed_fibers_are_collectible_before_root_close(self):
        async with Context() as root:
            references = []
            for _ in range(20):
                fiber = await root.use(lambda ctx, config: None)
                references.append(weakref.ref(fiber))
                await fiber.dispose()
            del fiber
            await asyncio.sleep(0)
            gc.collect()
            retained = sum(reference() is not None for reference in references)
        self.assertEqual(retained, 0)

    async def test_b07_invalid_yield_closes_suspended_generator(self):
        seen = []

        def initializer():
            try:
                yield lambda: seen.append("undo")
                yield 123  # Invalid inverse: the runtime rejects it.
            finally:
                seen.append("finally")

        iterator = initializer()  # Keep a reference so GC cannot hide the bug.
        async with Context() as root:
            try:
                with self.assertRaises(TypeError):
                    await root.effect(lambda: iterator)
                after_failure = seen.copy()
            finally:
                iterator.close()
        self.assertCountEqual(after_failure, ["finally", "undo"])

    async def test_b08_managed_task_rejects_dependency_handle_self_wait(self):
        outcomes = []
        jobs = []
        async with Context() as root:
            remove_database = root.provide("db", "value")

            @plugin(inject=["db"])
            def consumer(ctx, _config):
                async def job():
                    try:
                        # timeout keeps the same Task identity, unlike wait_for().
                        async with asyncio.timeout(0.1):
                            await remove_database()
                    except InactiveContextError:
                        outcomes.append("rejected")
                    except TimeoutError:
                        outcomes.append("deadlock")

                jobs.append(ctx.spawn(job()))

            await root.use(consumer)
            await asyncio.gather(*jobs)
            await root.flush()
        self.assertEqual(outcomes, ["rejected"])


class LoaderAudit(unittest.IsolatedAsyncioTestCase):
    async def test_k01_failed_update_does_not_suppress_same_config_retry(self):
        attempts = []

        @plugin(provide=["value"])
        def app(ctx, config):
            attempts.append(config)
            if config == 2:
                raise ValueError("bad configuration")
            ctx.provide("value", config)

        async with Context() as root:
            loader = Loader(root)
            await loader.reconcile([Entry("app", app, 1)])
            with self.assertRaises(ValueError):
                await loader.reconcile([Entry("app", app, 2)])
            first_count = attempts.count(2)
            try:
                await loader.reconcile([Entry("app", app, 2)])
            except ValueError:
                pass
            retried = attempts.count(2) > first_count
            actual = (loader.entries[0].config, loader.get("app").config, retried)
        self.assertEqual(actual, (1, 1, True))

    async def test_k02_import_failure_preserves_existing_entries(self):
        @plugin(provide=["value"])
        def app(ctx, _config):
            ctx.provide("value", "old")

        async with Context() as root:
            loader = Loader(root)
            await loader.reconcile([Entry("old", app)])
            with self.assertRaises(ValueError):
                await loader.reconcile([Entry("new", "invalid-locator")])
            actual = ([entry.id for entry in loader.entries], root.get("value"))
        self.assertEqual(actual, (["old"], "old"))


def module_source(version, *, fail=False):
    body = (
        "raise ValueError('new plugin failed')"
        if fail
        else "ctx.provide('version', VERSION)"
    )
    return (
        "from cordis import plugin\n"
        f"VERSION = {version!r}\n"
        "@plugin(provide=['version'])\n"
        "def app(ctx, config):\n"
        f"    {body}\n"
    )


class HMRAudit(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="cordis-audit-")
        self.package = "cordis_audit_20260910_fixture"
        self.module = self.package + ".app"
        directory = Path(self.temp.name) / self.package
        directory.mkdir()
        (directory / "__init__.py").write_text("", encoding="utf-8")
        self.path = directory / "app.py"
        self.path.write_text(module_source("v1"), encoding="utf-8")
        sys.path.insert(0, self.temp.name)
        importlib.invalidate_caches()

    async def asyncTearDown(self):
        for name in (self.module, self.package):
            sys.modules.pop(name, None)
        sys.path.remove(self.temp.name)
        self.temp.cleanup()

    async def test_b04_rollback_restores_parent_package_binding(self):
        async with Context() as root:
            loader = Loader(root)
            await loader.reconcile([Entry("app", f"{self.module}:app")])
            old_module = sys.modules[self.module]
            package = sys.modules[self.package]
            self.path.write_text(module_source("v2", fail=True), encoding="utf-8")
            with self.assertRaises(ExceptionGroup):
                await loader.reload("app")
            actual = (
                root.get("version"),
                sys.modules[self.module] is old_module,
                package.app is old_module,
                package.app.VERSION,
            )
        self.assertEqual(actual, ("v1", True, True, "v1"))

    async def test_b05_cancelled_reload_still_recovers_failed_replacement(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async with Context() as root:
            loader = Loader(root)
            await loader.reconcile(
                [
                    Entry("app", f"{self.module}:app", (entered, release)),
                ]
            )
            self.path.write_text(
                "from cordis import plugin\n"
                "@plugin(provide=['version'])\n"
                "async def app(ctx, config):\n"
                "    config[0].set()\n"
                "    await config[1].wait()\n"
                "    raise ValueError('replacement failed')\n",
                encoding="utf-8",
            )
            reload_task = asyncio.create_task(loader.reload("app"))
            await entered.wait()
            reload_task.cancel()
            # Permit either rollback-on-cancel or a shielded transaction to finish.
            release.set()
            try:
                await reload_task
            except (asyncio.CancelledError, ExceptionGroup):
                pass
            await root.flush()
            actual = (root.get("version"), loader.get("app").state)
        self.assertEqual(actual, ("v1", FiberState.ACTIVE))

    async def test_l01_nested_hmr_reloads_from_root(self):
        async with Context() as root:
            loader = Loader(root)
            await loader.reconcile(
                [
                    Entry("group", children=(Entry("app", f"{self.module}:app"),)),
                ]
            )
            self.path.write_text(module_source("version two"), encoding="utf-8")
            reloaded = await loader.reload_changed([self.path])
            actual = (reloaded, root.get("version"))
        self.assertEqual(actual, (("group/app",), "version two"))
