import unittest

from cordis import Entry, FiberState, Loader, plugin
from cordis import Context
from . import sample_plugins


class LoaderTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        sample_plugins.events.clear()

    async def test_reconcile_add_update_remove(self):
        root = Context()
        loader = Loader(root)
        locator = "tests.sample_plugins:configured"

        await loader.reconcile([Entry("sample", locator, {"value": 1})])
        self.assertEqual(root.fibers[0].state, FiberState.ACTIVE)

        await loader.reconcile([Entry("sample", locator, {"value": 2})])
        await loader.reconcile([])
        self.assertEqual(
            sample_plugins.events,
            [
                ("start", {"value": 1}),
                ("stop", {"value": 1}),
                ("start", {"value": 2}),
                ("stop", {"value": 2}),
            ],
        )

    async def test_duplicate_ids_are_rejected(self):
        root = Context()
        loader = Loader(root)
        with self.assertRaisesRegex(ValueError, "duplicate entry ids"):
            await loader.reconcile(
                [
                    Entry("same", sample_plugins.configured),
                    Entry("same", sample_plugins.configured),
                ]
            )

    async def test_managed_realms_are_stable_and_reassignable(self):
        values = []

        @plugin(provide=["value"])
        def provider(ctx, config):
            ctx.provide("value", config)

        @plugin(inject=["value"])
        def consumer(ctx, _config):
            values.append(f"start:{ctx.value}")
            return lambda: values.append(f"stop:{ctx.value}")

        root = Context()
        loader = Loader(root)
        await loader.reconcile(
            [
                Entry("provider", provider, "one", isolate={"value": "shared"}),
                Entry("consumer", consumer, isolate={"value": "shared"}),
            ]
        )
        provider_fiber = loader.get("provider")
        self.assertEqual(values, ["start:one"])

        await loader.reconcile(
            [
                Entry("provider", provider, "one", isolate={"value": "moved"}),
                Entry("consumer", consumer, isolate={"value": "shared"}),
            ]
        )
        self.assertIs(loader.get("provider"), provider_fiber)
        self.assertEqual(loader.get("consumer").state, FiberState.PENDING)
        self.assertEqual(values, ["start:one", "stop:one"])

        await loader.reconcile(
            [
                Entry("provider", provider, "one", isolate={"value": "moved"}),
                Entry("consumer", consumer, isolate={"value": "moved"}),
            ]
        )
        self.assertEqual(loader.get("consumer").state, FiberState.ACTIVE)
        self.assertEqual(values, ["start:one", "stop:one", "start:one"])

    async def test_intercept_updates_without_reloading(self):
        contexts = []

        @plugin(provide=["value"])
        def provider(ctx, _config):
            ctx.provide(
                "value",
                "cordis",
                adapt=lambda value, metadata, _ctx: metadata["prefix"] + value,
            )

        @plugin(inject=["value"])
        def consumer(ctx, _config):
            contexts.append(ctx)

        root = Context()
        loader = Loader(root)
        await loader.reconcile(
            [
                Entry("provider", provider),
                Entry("consumer", consumer, intercept={"value": {"prefix": "a-"}}),
            ]
        )
        fiber = loader.get("consumer")
        self.assertEqual(contexts[0].value, "a-cordis")

        await loader.reconcile(
            [
                Entry("provider", provider),
                Entry("consumer", consumer, intercept={"value": {"prefix": "b-"}}),
            ]
        )
        self.assertIs(loader.get("consumer"), fiber)
        self.assertEqual(contexts[0].value, "b-cordis")

    async def test_nested_group_entries_follow_group_lifetime(self):
        events = []

        @plugin
        def child(_ctx, name):
            events.append(f"{name}:start")
            return lambda: events.append(f"{name}:stop")

        root = Context()
        loader = Loader(root)
        first = Entry("first", child, "first")
        await loader.reconcile([Entry("parent", children=(first,))])
        self.assertEqual(events, ["first:start"])

        second = Entry("second", child, "second")
        await loader.reconcile([Entry("parent", children=(first, second))])
        self.assertEqual(events, ["first:start", "second:start"])

        await loader.reconcile([])
        self.assertEqual(
            events,
            ["first:start", "second:start", "first:stop", "second:stop"],
        )


if __name__ == "__main__":
    unittest.main()
