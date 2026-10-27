"""Production regressions for configuration ownership and failed persistence."""

import asyncio
import json
import importlib
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import patch

from cordis import (
    ConfigError,
    ConfigIncludes,
    ConfigPatch,
    ConfigStore,
    Context,
    Entry,
    FiberState,
    FileBackend,
    JsonCodec,
    Loader,
    plugin,
)


LOCATOR = "tests.sample_plugins:configured"


def document(config=1):
    return json.dumps([{"id": "worker", "plugin": LOCATOR, "config": config}])


class MemoryBackend:
    identity = "production-config"

    def __init__(self, text="[]"):
        self.text = text
        self.fail = False
        self.writes = 0

    async def read(self):
        return self.text

    async def write(self, text):
        if self.fail:
            raise OSError("storage unavailable")
        self.writes += 1
        self.text = text

    async def delete(self):
        self.text = None


class ConfigurationOwnershipTests(unittest.IsolatedAsyncioTestCase):
    async def test_dependency_lost_before_driver_starts_remains_pending(self):
        available = True
        started = []

        @plugin(inject=["database"])
        def consumer(ctx, config):
            started.append(ctx.service("database"))

        async with Context() as root:
            root.provide("database", "db", check=lambda _: available)
            fiber = root.use(consumer)
            available = False
            root.notify("database")
            await fiber.wait(raise_error=False)
            self.assertEqual(fiber.state, FiberState.PENDING)
            self.assertIsNone(fiber.error)
            self.assertEqual(started, [])
            available = True
            root.notify("database")
            await fiber
            self.assertEqual(started, ["db"])
            self.assertEqual(root.errors, ())

    async def test_cancelled_include_finishes_registration_and_can_be_removed(self):
        entered, release = asyncio.Event(), asyncio.Event()

        class BlockingBackend(MemoryBackend):
            async def read(self):
                entered.set()
                await release.wait()
                return await super().read()

        async with Context() as root:
            loader = Loader(root)
            includes = ConfigIncludes(loader)
            creating = asyncio.create_task(
                includes.include("tenant", BlockingBackend(document()), JsonCodec())
            )
            try:
                await entered.wait()
                creating.cancel()
            finally:
                release.set()
            with self.assertRaises(asyncio.CancelledError):
                await creating
            self.assertEqual(includes.names, ("tenant",))
            self.assertEqual(loader.get("tenant/worker").state, FiberState.ACTIVE)
            await includes.remove("tenant")
            self.assertEqual(root.fibers, ())

    async def test_cancelled_nested_disposal_finishes_persistence(self):
        entered, release = asyncio.Event(), asyncio.Event()

        class BlockingBackend(MemoryBackend):
            async def write(self, text):
                entered.set()
                await release.wait()
                await super().write(text)

        async with Context() as root:
            loader = Loader(root)
            backend = BlockingBackend(
                json.dumps([{"id": "group", "children": json.loads(document())}])
            )
            store = ConfigStore(loader, backend, JsonCodec())
            await store.load()
            disposing = asyncio.create_task(loader.get("group/worker").dispose())
            try:
                await entered.wait()
                disposing.cancel()
            finally:
                release.set()
            with self.assertRaises(asyncio.CancelledError):
                await disposing
            self.assertFalse(store.tree.get("group/worker").enabled)
            self.assertFalse(json.loads(backend.text)[0]["children"][0]["enabled"])

    async def test_include_remains_reloadable_through_parent_hmr(self):
        from cordis.hmr import HotReloader

        with tempfile.TemporaryDirectory() as directory:
            module = "cordis_include_production_test"
            Path(directory, module + ".py").write_text(
                "from cordis import plugin\n@plugin\ndef worker(ctx, config): pass\n",
                encoding="utf-8",
            )
            sys.path.insert(0, directory)
            try:
                async with Context() as root:
                    loader = Loader(root)
                    main = ConfigStore(loader, MemoryBackend(), JsonCodec())
                    await main.load()
                    includes = ConfigIncludes(loader)
                    tenant = await includes.include(
                        "tenant",
                        MemoryBackend(
                            json.dumps(
                                [
                                    {
                                        "id": "worker",
                                        "plugin": module + ":worker",
                                    }
                                ]
                            )
                        ),
                        JsonCodec(),
                    )
                    original = tenant.loader.get("worker")
                    await HotReloader(loader).reload_entries(["tenant/worker"])
                    self.assertEqual(original.state, FiberState.DISPOSED)
                    self.assertEqual(
                        tenant.loader.get("worker").state, FiberState.ACTIVE
                    )
                    await main.save()
                    self.assertEqual(
                        tenant.loader.get("worker").state, FiberState.ACTIVE
                    )
            finally:
                sys.path.remove(directory)
                sys.modules.pop(module, None)

    async def test_include_consumer_failure_rolls_back_main_provider(self):
        @plugin(provide=["database"])
        def provider(ctx, config):
            ctx.provide("database", config)

        @plugin(inject=["database"])
        def consumer(ctx, config):
            if ctx.service("database") == "bad":
                raise ValueError("consumer rejected provider")

        sample = importlib.import_module("tests.sample_plugins")
        with (
            patch.object(sample, "provider", provider, create=True),
            patch.object(sample, "consumer", consumer, create=True),
        ):
            async with Context() as root:
                loader = Loader(root)
                main = ConfigStore(
                    loader,
                    MemoryBackend(
                        json.dumps(
                            [
                                {
                                    "id": "db",
                                    "plugin": "tests.sample_plugins:provider",
                                    "config": "good",
                                }
                            ]
                        )
                    ),
                    JsonCodec(),
                )
                await main.load()
                includes = ConfigIncludes(loader)
                tenant = await includes.include(
                    "tenant",
                    MemoryBackend(
                        json.dumps(
                            [
                                {
                                    "id": "reader",
                                    "plugin": "tests.sample_plugins:consumer",
                                }
                            ]
                        )
                    ),
                    JsonCodec(),
                )
                reader = tenant.loader.get("reader")
                with self.assertRaisesRegex(ValueError, "consumer rejected"):
                    await main.update("db", config="bad")
                self.assertEqual(main.tree.get("db").config, "good")
                self.assertIs(loader.child("tenant"), tenant.loader)
                self.assertIs(tenant.loader.get("reader"), reader)
                self.assertEqual(reader.state, FiberState.ACTIVE)

    async def test_main_store_save_preserves_include_and_its_store(self):
        async with Context() as root:
            loader = Loader(root)
            main = ConfigStore(loader, MemoryBackend(), JsonCodec())
            await main.load()
            includes = ConfigIncludes(loader)
            backend = MemoryBackend(document())
            tenant = await includes.include("tenant", backend, JsonCodec())
            worker = tenant.loader.get("worker")
            await main.save()
            self.assertEqual(worker.state, FiberState.ACTIVE)
            self.assertIs(loader.child("tenant"), tenant.loader)
            self.assertIn(tenant.loader, tuple(loader.walk()))
            await tenant.update("worker", config=2)
            await main.load()
            self.assertIs(loader.get("tenant/worker"), tenant.loader.get("worker"))
            self.assertEqual(json.loads(backend.text)[0]["config"], 2)
            await includes.remove("tenant")
            self.assertEqual(includes.names, ())
            self.assertEqual(worker.state, FiberState.DISPOSED)

    async def test_include_failure_leaves_other_entries_and_includes_untouched(self):
        async with Context() as root:
            loader = Loader(root)
            main = ConfigStore(loader, MemoryBackend(document()), JsonCodec())
            await main.load()
            includes = ConfigIncludes(loader)
            first = await includes.include(
                "first", MemoryBackend(document()), JsonCodec()
            )
            original = main.tree
            with self.assertRaises(json.JSONDecodeError):
                await includes.include("bad", MemoryBackend("invalid"), JsonCodec())
            self.assertEqual(includes.names, ("first",))
            self.assertEqual(main.tree, original)
            self.assertEqual(first.loader.get("worker").state, FiberState.ACTIVE)
            self.assertIsNone(loader.child("bad"))
            await main.save()
            self.assertEqual(first.loader.get("worker").state, FiberState.ACTIVE)

    async def test_nested_disposal_persists_full_path_and_survives_reload(self):
        async with Context() as root:
            loader = Loader(root)
            backend = MemoryBackend(
                json.dumps(
                    [
                        {
                            "id": "outer",
                            "children": [
                                {
                                    "id": "inner",
                                    "children": json.loads(document()),
                                }
                            ],
                        }
                    ]
                )
            )
            store = ConfigStore(loader, backend, JsonCodec())
            await store.load()
            await loader.get("outer/inner/worker").dispose()
            self.assertFalse(store.tree.get("outer/inner/worker").enabled)
            self.assertFalse(
                json.loads(backend.text)[0]["children"][0]["children"][0]["enabled"]
            )
            await store.load()
            self.assertIsNone(loader.get("outer/inner/worker"))

    async def test_nested_runtime_update_persists_without_recreating_siblings(self):
        async with Context() as root:
            loader = Loader(root)
            backend = MemoryBackend(
                json.dumps(
                    [
                        {
                            "id": "group",
                            "children": [
                                *json.loads(document()),
                                {"id": "sibling", "plugin": LOCATOR, "config": 9},
                            ],
                        }
                    ]
                )
            )
            store = ConfigStore(loader, backend, JsonCodec())
            await store.load()
            sibling = loader.get("group/sibling")
            await loader.get("group/worker").update(2)
            self.assertEqual(store.tree.get("group/worker").config, 2)
            self.assertEqual(json.loads(backend.text)[0]["children"][0]["config"], 2)
            self.assertIs(loader.get("group/sibling"), sibling)

    async def test_nested_update_write_failure_restores_configuration(self):
        async with Context() as root:
            loader = Loader(root)
            backend = MemoryBackend(
                json.dumps(
                    [
                        {
                            "id": "group",
                            "children": json.loads(document()),
                        }
                    ]
                )
            )
            store = ConfigStore(loader, backend, JsonCodec())
            await store.load()
            backend.fail = True
            with self.assertRaises(OSError):
                await loader.get("group/worker").update(2)
            self.assertEqual(store.tree.get("group/worker").config, 1)
            self.assertEqual(loader.get("group/worker").config, 1)


class FailedPersistenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_read_only_unchanged_sidecar_does_not_mask_write_error(self):
        async with Context() as root:
            backend, patches = MemoryBackend(document()), MemoryBackend("[]")
            store = ConfigStore(
                Loader(root), backend, JsonCodec(), patch_backend=patches
            )
            await store.load()
            patches.fail = True
            with self.assertRaisesRegex(OSError, "storage unavailable"):
                await store.update("worker", config=2)
            self.assertEqual(json.loads(backend.text)[0]["config"], 1)
            self.assertEqual(store.tree.get("worker").config, 1)

    async def test_partial_sidecar_failure_restores_both_backends(self):
        class PartialBackend(MemoryBackend):
            async def write(self, text):
                self.text = text
                if self.fail:
                    self.fail = False
                    raise OSError("write failed after commit")

        async with Context() as root:
            backend, patches = MemoryBackend(document()), PartialBackend("[]")
            store = ConfigStore(
                Loader(root), backend, JsonCodec(), patch_backend=patches
            )
            await store.load()
            patches.fail = True
            with self.assertRaisesRegex(OSError, "after commit"):
                await store.update("worker", config=2)
            self.assertEqual(json.loads(backend.text)[0]["config"], 1)
            self.assertEqual(patches.text, "[]")
            await store.update("worker", config=2)
            self.assertEqual(store.tree.get("worker").config, 2)

    async def test_recovery_attempts_sidecar_even_if_main_restore_fails(self):
        class MainBackend(MemoryBackend):
            async def write(self, text):
                if self.writes:
                    raise OSError("main recovery failed")
                await super().write(text)

        class PartialBackend(MemoryBackend):
            async def write(self, text):
                self.text = text
                if self.fail:
                    self.fail = False
                    raise OSError("sidecar write failed")

        async with Context() as root:
            backend, patches = MainBackend(document()), PartialBackend("[]")
            store = ConfigStore(
                Loader(root), backend, JsonCodec(), patch_backend=patches
            )
            await store.load()
            patches.fail = True
            with self.assertRaises(ExceptionGroup) as caught:
                await store.update("worker", config=2)
            self.assertEqual(
                [str(error) for error in caught.exception.exceptions],
                [
                    "sidecar write failed",
                    "main recovery failed",
                ],
            )
            self.assertEqual(patches.text, "[]")
            self.assertEqual(store.tree.get("worker").config, 1)

    async def test_deferred_merge_keeps_return_tree_journal_and_runtime_consistent(
        self,
    ):
        async with Context() as root:
            loader = Loader(root)
            backend = MemoryBackend(document())
            store = ConfigStore(
                loader, backend, JsonCodec(), conflicts="file_wins", deferred=True
            )
            await store.load()
            backend.text = document(3)
            backend.fail = True
            result = await store.update("worker", config=2)
            self.assertEqual(result.get("worker").config, 3)
            self.assertEqual(store.tree.get("worker").config, 3)
            self.assertEqual(loader.get("worker").config, 3)
            self.assertEqual(store.journal[0].after.config, 3)
            self.assertIsInstance(store.write_error, OSError)
            backend.fail = False
            await store.flush()
            self.assertEqual(json.loads(backend.text)[0]["config"], 3)
            self.assertEqual(store.journal, ())
            self.assertIsNone(store.write_error)

    async def test_deleting_sidecar_removes_old_overrides(self):
        async with Context() as root:
            loader = Loader(root)
            patches = MemoryBackend('[{"path":["worker"],"changes":{"config":2}}]')
            store = ConfigStore(
                loader, MemoryBackend(document()), JsonCodec(), patch_backend=patches
            )
            await store.load()
            self.assertEqual(store.tree.get("worker").config, 2)
            await patches.delete()
            await store.load()
            self.assertEqual(store.tree.get("worker").config, 1)
            self.assertEqual(loader.get("worker").config, 1)
            self.assertEqual(store.patches, ())

    async def test_missing_sidecar_falls_back_to_constructor_patches(self):
        async with Context() as root:
            loader = Loader(root)
            patches = MemoryBackend('[{"path":["worker"],"changes":{"config":3}}]')
            store = ConfigStore(
                loader,
                MemoryBackend(document()),
                JsonCodec(),
                patch_backend=patches,
                patches=[ConfigPatch("worker", {"config": 2})],
            )
            await store.load()
            await patches.delete()
            await store.load()
            self.assertEqual(store.tree.get("worker").config, 2)

    async def test_first_sidecar_failure_restores_absence_and_allows_retry(self):
        async with Context() as root:
            backend, patches = MemoryBackend(None), MemoryBackend(None)
            store = ConfigStore(
                Loader(root), backend, JsonCodec(), patch_backend=patches
            )
            patches.fail = True
            with self.assertRaises(OSError):
                await store.create(Entry("worker", LOCATOR, 2))
            self.assertIsNone(backend.text)
            self.assertIsNone(patches.text)
            self.assertEqual(store.tree.entries, ())
            patches.fail = False
            await store.create(Entry("worker", LOCATOR, 2))
            self.assertEqual(store.tree.get("worker").config, 2)

    async def test_unsafe_creation_is_rejected_before_writing(self):
        class ReadWriteOnly:
            identity = "read-write-only"
            text = None

            async def read(self):
                return self.text

            async def write(self, text):
                self.text = text

        async with Context() as root:
            backend = ReadWriteOnly()
            store = ConfigStore(
                Loader(root), backend, JsonCodec(), patch_backend=MemoryBackend(None)
            )
            with self.assertRaises(ConfigError):
                await store.create(Entry("worker", LOCATOR))
            self.assertIsNone(backend.text)
            self.assertEqual(store.tree.entries, ())

    async def test_file_backend_can_restore_missing_file(self):
        with tempfile.TemporaryDirectory() as directory:
            async with Context() as root:
                path = Path(directory) / "config.json"
                patches = MemoryBackend(None)
                store = ConfigStore(
                    Loader(root), FileBackend(path), JsonCodec(), patch_backend=patches
                )
                patches.fail = True
                with self.assertRaises(OSError):
                    await store.create(Entry("worker", LOCATOR))
                self.assertFalse(path.exists())


class EagerLifecycleTests(unittest.TestCase):
    @unittest.skipUnless(
        hasattr(asyncio, "eager_task_factory"), "requires Python 3.12+"
    )
    def test_eager_start_restart_failure_and_shutdown_keep_event_loop_live(self):
        source = textwrap.dedent("""
            import asyncio
            from cordis import Context, FiberState, InactiveContextError, plugin

            async def main():
                loop = asyncio.get_running_loop()
                loop.set_task_factory(asyncio.eager_task_factory)
                seen = []
                @plugin
                def worker(ctx, config):
                    if config == "bad":
                        raise ValueError("bad")
                    seen.append(config)
                async with Context() as root:
                    fiber = await root.use(worker, 1)
                    async def owned():
                        try:
                            await root.wait_ready()
                        except InactiveContextError:
                            return
                        raise AssertionError("eager work lost its owner")
                    await root.spawn(owned())
                    await fiber.restart()
                    await fiber.update(2)
                    try:
                        await fiber.update("bad")
                    except ValueError:
                        pass
                    else:
                        raise AssertionError("startup failure was lost")
                    assert fiber.config == 2
                    assert fiber.state is FiberState.ACTIVE
                    await root.wait_ready(timeout=1)
                assert seen == [1, 1, 2, 2], seen
            asyncio.run(main())
        """)
        result = subprocess.run(
            [sys.executable, "-W", "error::RuntimeWarning", "-c", source],
            capture_output=True,
            text=True,
            timeout=5,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
