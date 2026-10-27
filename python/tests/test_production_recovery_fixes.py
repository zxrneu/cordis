"""Recovery must preserve configuration paths and unrelated scope owners."""

import asyncio
import json
import unittest
from unittest.mock import patch

from cordis import (
    ConfigIncludes,
    ConfigStore,
    Context,
    Entry,
    EntryTree,
    FiberState,
    InactiveContextError,
    JsonCodec,
    Loader,
    plugin,
)
from tests import sample_plugins


LOCATOR = "tests.sample_plugins:configured"


class MemoryBackend:
    identity = "production-recovery"

    def __init__(self, text="[]"):
        self.text = text
        self.fail = False

    async def read(self):
        return self.text

    async def write(self, text):
        if self.fail:
            raise OSError("storage unavailable")
        self.text = text


def leaves(error):
    if isinstance(error, BaseExceptionGroup):
        return [leaf for child in error.exceptions for leaf in leaves(child)]
    return [error]


class PatchPathTests(unittest.IsolatedAsyncioTestCase):
    async def store(self, *, inserted=False, deferred=False, backend=None):
        root = Context()
        self.addAsyncCleanup(root.close)
        worker = Entry("worker", LOCATOR, 1)
        entries = (
            Entry("left", children=() if inserted else (worker,)),
            Entry("right", children=(Entry("worker", LOCATOR, 2),)),
            Entry("destination"),
            Entry("other"),
        )
        codec = JsonCodec()
        backend = backend or MemoryBackend()
        backend.text = codec.dumps(entries)
        patches = MemoryBackend(
            json.dumps(
                [
                    {
                        "path": ["left"],
                        "insert": [{"id": "worker", "plugin": LOCATOR, "config": 9}],
                    }
                    if inserted
                    else {"path": ["left", "worker"], "changes": {"config": 9}}
                ]
            )
        )
        store = ConfigStore(
            Loader(root), backend, codec, patch_backend=patches, deferred=deferred
        )
        await store.load()
        return store, backend, patches

    async def test_removing_override_does_not_retarget_other_group(self):
        store, backend, patches = await self.store()
        await store.remove("left/worker")
        raw = EntryTree.from_values(json.loads(backend.text))
        self.assertEqual(raw.get("right/worker").config, 2)
        self.assertEqual(json.loads(patches.text), [])
        await store.load()
        self.assertEqual(store.loader.get("right/worker").config, 2)

    async def test_removing_insert_does_not_remove_file_owned_namesake(self):
        store, backend, patches = await self.store(inserted=True)
        await store.remove("left/worker")
        raw = EntryTree.from_values(json.loads(backend.text))
        self.assertEqual(raw.get("right/worker").config, 2)
        self.assertEqual(json.loads(patches.text), [])

    async def test_removing_group_discards_only_its_descendant_patch(self):
        store, backend, patches = await self.store()
        await store.remove("left")
        self.assertEqual(
            EntryTree.from_values(json.loads(backend.text)).get("right/worker").config,
            2,
        )
        self.assertEqual(json.loads(patches.text), [])

    async def test_full_tree_replace_does_not_infer_move_from_local_id(self):
        store, backend, patches = await self.store()
        await store.replace(store.tree.move("left/worker", parent="destination"))
        raw = EntryTree.from_values(json.loads(backend.text))
        self.assertEqual(raw.get("destination/worker").config, 9)
        self.assertEqual(raw.get("right/worker").config, 2)
        self.assertEqual(json.loads(patches.text), [])

    async def test_explicit_move_preserves_override_with_repeated_local_ids(self):
        store, backend, patches = await self.store()
        await store.move("left/worker", parent="destination")
        raw = EntryTree.from_values(json.loads(backend.text))
        self.assertEqual(raw.get("destination/worker").config, 1)
        self.assertEqual(raw.get("right/worker").config, 2)
        self.assertEqual(json.loads(patches.text)[0]["path"], ["destination", "worker"])
        await store.load()
        self.assertEqual(store.loader.get("destination/worker").config, 9)

    async def test_group_move_relocates_descendant_patch(self):
        store, backend, patches = await self.store()
        await store.move("left", parent="destination")
        raw = EntryTree.from_values(json.loads(backend.text))
        self.assertEqual(raw.get("destination/left/worker").config, 1)
        self.assertEqual(
            json.loads(patches.text)[0]["path"], ["destination", "left", "worker"]
        )
        await store.load()
        self.assertEqual(store.loader.get("destination/left/worker").config, 9)

    async def test_deferred_moves_keep_ownership_through_retry(self):
        store, backend, patches = await self.store(deferred=True)
        backend.fail = True
        await store.move("left/worker", parent="destination")
        await store.move("destination/worker", parent="other")
        self.assertEqual(store.tree.get("other/worker").config, 9)
        self.assertTrue(store.journal)
        backend.fail = False
        await store.flush()
        self.assertEqual(
            EntryTree.from_values(json.loads(backend.text)).get("other/worker").config,
            1,
        )
        self.assertEqual(json.loads(patches.text)[0]["path"], ["other", "worker"])
        await store.load()
        self.assertEqual(store.loader.get("other/worker").config, 9)

    async def test_failed_move_restores_old_patch_location(self):
        store, backend, patches = await self.store()
        backend.fail = True
        with self.assertRaises(OSError):
            await store.move("left/worker", parent="destination")
        self.assertEqual(store.tree.get("left/worker").config, 9)
        backend.fail = False
        await store.update("left/worker", config=10)
        self.assertEqual(json.loads(patches.text)[0]["path"], ["left", "worker"])
        self.assertEqual(
            EntryTree.from_values(json.loads(backend.text)).get("left/worker").config, 1
        )

    async def test_failed_removal_retains_original_patch_ownership(self):
        store, backend, patches = await self.store()
        backend.fail = True
        with self.assertRaises(OSError):
            await store.remove("left/worker")
        backend.fail = False
        await store.update("left/worker", config=10)
        raw = EntryTree.from_values(json.loads(backend.text))
        self.assertEqual(raw.get("left/worker").config, 1)
        self.assertEqual(json.loads(patches.text)[0]["changes"], {"config": 10})
        await store.load()
        self.assertEqual(store.loader.get("left/worker").config, 10)

    async def test_deferred_delete_then_recreate_does_not_resurrect_patch(self):
        store, backend, patches = await self.store(deferred=True)
        backend.fail = True
        await store.move("left/worker", parent="destination")
        await store.remove("destination/worker")
        await store.create(Entry("worker", LOCATOR, 5), parent="destination")
        backend.fail = False
        await store.flush()
        self.assertEqual(json.loads(patches.text), [])
        self.assertEqual(
            EntryTree.from_values(json.loads(backend.text))
            .get("destination/worker")
            .config,
            5,
        )

    async def test_deferred_delete_then_recreate_old_path_discards_patch(self):
        store, backend, patches = await self.store(deferred=True)
        backend.fail = True
        await store.remove("left/worker")
        await store.create(Entry("worker", LOCATOR, 5), parent="left")
        backend.fail = False
        await store.flush()
        self.assertEqual(json.loads(patches.text), [])
        self.assertEqual(
            EntryTree.from_values(json.loads(backend.text)).get("left/worker").config, 5
        )

    async def test_insert_move_stays_in_sidecar(self):
        store, backend, patches = await self.store(inserted=True)
        await store.move("left/worker", parent="destination")
        raw = EntryTree.from_values(json.loads(backend.text))
        self.assertEqual(raw.get("destination").children, ())
        self.assertEqual(raw.get("right/worker").config, 2)
        self.assertEqual(json.loads(patches.text)[0]["path"], ["destination"])
        await store.load()
        self.assertEqual(store.loader.get("destination/worker").config, 9)

    async def test_cancelled_move_commits_patch_path_before_returning(self):
        entered, release = asyncio.Event(), asyncio.Event()

        class BlockingBackend(MemoryBackend):
            async def write(self, text):
                entered.set()
                await release.wait()
                await super().write(text)

        store, backend, patches = await self.store(backend=BlockingBackend())
        moving = asyncio.create_task(store.move("left/worker", parent="destination"))
        try:
            await entered.wait()
            moving.cancel()
        finally:
            release.set()
        with self.assertRaises(asyncio.CancelledError):
            await moving
        self.assertEqual(json.loads(patches.text)[0]["path"], ["destination", "worker"])
        await store.load()
        self.assertEqual(store.loader.get("destination/worker").config, 9)


class NestedRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def scenario(self, *, recovery_fails=False, gate=None):
        root = Context()
        self.addAsyncCleanup(root.close)
        attempts = []

        @plugin(provide=["database"])
        def database(ctx, config):
            ctx.provide("database", config)

        @plugin(inject=["database"])
        async def reader(ctx, config):
            value = ctx.service("database")
            attempts.append(value)
            if value == "bad":
                if gate:
                    gate[0].set()
                    await gate[1].wait()
                raise ValueError("reader rejected provider")
            if recovery_fails and len(attempts) > 1:
                raise RuntimeError("reader recovery failed")

        @plugin
        def extra(ctx, config):
            pass

        loader = Loader(root)
        group = Entry(
            "outer", children=(Entry("inner", children=(Entry("reader", reader),)),)
        )
        original = [Entry("database", database, "good"), group]
        await loader.reconcile(original)
        outer, child = loader.child("outer"), loader.child("outer").child("inner")
        typed = await child.mount("extra", extra.configure(None))
        consumer = child.get("reader")
        return (
            root,
            loader,
            outer,
            child,
            typed,
            consumer,
            [Entry("database", database, "bad"), group],
        )

    async def test_declared_failure_preserves_unchanged_group_and_dynamic_mount(self):
        root, loader, outer, child, typed, consumer, changed = await self.scenario()
        with self.assertRaisesRegex(ValueError, "reader rejected"):
            await loader.reconcile(changed)
        self.assertIs(loader.child("outer"), outer)
        self.assertIs(loader.child("outer").child("inner"), child)
        self.assertIs(child.get("extra"), typed)
        self.assertIs(child.get("reader"), consumer)
        self.assertEqual(typed.state, FiberState.ACTIVE)
        self.assertEqual(consumer.state, FiberState.ACTIVE)
        self.assertEqual(root.service("database"), "good")

    async def test_recovery_failure_preserves_dynamic_mount_and_both_errors(self):
        root, loader, outer, child, typed, consumer, changed = await self.scenario(
            recovery_fails=True
        )
        with self.assertRaises(ExceptionGroup) as caught:
            await loader.reconcile(changed)
        self.assertEqual(
            {str(error) for error in leaves(caught.exception)},
            {
                "reader rejected provider",
                "reader recovery failed",
            },
        )
        self.assertIs(loader.child("outer"), outer)
        self.assertIs(child.get("extra"), typed)
        self.assertEqual(typed.state, FiberState.ACTIVE)
        self.assertEqual(consumer.state, FiberState.FAILED)

    async def test_cancelled_failed_update_still_recovers_original_owners(self):
        entered, release = asyncio.Event(), asyncio.Event()
        root, loader, outer, child, typed, consumer, changed = await self.scenario(
            gate=(entered, release)
        )
        updating = asyncio.create_task(loader.reconcile(changed))
        try:
            await entered.wait()
            updating.cancel()
        finally:
            release.set()
        with self.assertRaises(asyncio.CancelledError) as caught:
            await updating
        self.assertIsInstance(caught.exception.__cause__, ValueError)
        self.assertIs(loader.child("outer"), outer)
        self.assertIs(child.get("extra"), typed)
        self.assertEqual(consumer.state, FiberState.ACTIVE)


class IncludeRemovalTests(unittest.IsolatedAsyncioTestCase):
    async def test_failed_cleanup_unregisters_include_and_allows_recreation(self):
        @plugin
        def broken(ctx, config):
            def close():
                raise ValueError("tenant cleanup failed")

            return close

        root = Context()
        try:
            with patch.object(sample_plugins, "broken", broken, create=True):
                loader, backend = (
                    Loader(root),
                    MemoryBackend(
                        '[{"id":"worker","plugin":"tests.sample_plugins:broken"}]'
                    ),
                )
                includes = ConfigIncludes(loader)
                await includes.include("tenant", backend, JsonCodec())
                with self.assertRaises(ExceptionGroup) as caught:
                    await includes.remove("tenant")
                self.assertIn(
                    "tenant cleanup failed",
                    {str(error) for error in leaves(caught.exception)},
                )
                self.assertEqual(includes.names, ())
                self.assertIsNone(loader.child("tenant"))
                store = await includes.include("tenant", MemoryBackend(), JsonCodec())
                self.assertIs(loader.child("tenant"), store.loader)
                await includes.remove("tenant")
        finally:
            with self.assertRaises(ExceptionGroup):
                await root.close()

    async def test_cancelled_failed_cleanup_still_unregisters_include(self):
        entered, release = asyncio.Event(), asyncio.Event()

        @plugin
        def broken(ctx, config):
            async def close():
                entered.set()
                await release.wait()
                raise ValueError("tenant cleanup failed")

            return close

        root = Context()
        try:
            with patch.object(sample_plugins, "broken", broken, create=True):
                loader = Loader(root)
                includes = ConfigIncludes(loader)
                await includes.include(
                    "tenant",
                    MemoryBackend(
                        '[{"id":"worker","plugin":"tests.sample_plugins:broken"}]'
                    ),
                    JsonCodec(),
                )
                removing = asyncio.create_task(includes.remove("tenant"))
                try:
                    await entered.wait()
                    removing.cancel()
                finally:
                    release.set()
                with self.assertRaises(asyncio.CancelledError) as caught:
                    await removing
                self.assertIsInstance(caught.exception.__cause__, ExceptionGroup)
                self.assertEqual(includes.names, ())
                self.assertIsNone(loader.child("tenant"))
        finally:
            release.set()
            with self.assertRaises(ExceptionGroup):
                await root.close()

    async def test_self_wait_rejection_preserves_include_registration(self):
        async with Context() as root:
            loader = Loader(root)
            includes = ConfigIncludes(loader)
            store = await includes.include(
                "tenant",
                MemoryBackend(
                    json.dumps(
                        [
                            {"id": "worker", "plugin": LOCATOR},
                        ]
                    )
                ),
                JsonCodec(),
            )
            worker = store.loader.get("worker")

            async def request():
                with self.assertRaises(InactiveContextError):
                    await includes.remove("tenant")

            await worker.ctx.spawn(request())
            self.assertEqual(includes.names, ("tenant",))
            self.assertEqual(worker.state, FiberState.ACTIVE)
            await includes.remove("tenant")
            self.assertEqual(includes.names, ())
