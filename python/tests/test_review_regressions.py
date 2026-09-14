"""Regression tests for the 2026-09-14 runtime review."""

import unittest

from cordis import Context, FiberState, plugin


class RealmReviewTests(unittest.IsolatedAsyncioTestCase):
    async def test_provider_can_publish_same_name_in_multiple_realms(self):
        realms = [object(), object()]
        seen = []

        @plugin
        def provider(ctx, config):
            for index, realm in enumerate(realms):
                ctx.isolate("db", realm=realm).provide("db", index)

        @plugin(inject=["db"])
        def consumer(ctx, config):
            seen.append(ctx.db)

        async with Context() as root:
            consumers = [root.isolate("db", realm=r).use(consumer) for r in realms]
            unrelated = root.isolate("db").use(consumer)
            await root.use(provider)
            await root.flush()
            self.assertCountEqual(seen, [0, 1])
            self.assertTrue(all(f.state is FiberState.ACTIVE for f in consumers))
            self.assertEqual(unrelated.state, FiberState.PENDING)

    async def test_derived_provider_wakes_waiting_consumer(self):
        realm = object()
        seen = []

        @plugin(inject=["db"])
        def consumer(ctx, config):
            seen.append(ctx.db)

        @plugin
        def provider(ctx, config):
            ctx.isolate("db", realm=realm).provide("db", "ready")

        async with Context() as root:
            fiber = root.isolate("db", realm=realm).use(consumer)
            await root.use(provider)
            await root.flush()
            self.assertEqual(fiber.state, FiberState.ACTIVE)
            self.assertEqual(seen, ["ready"])

    async def test_derived_provider_waits_for_consumer_before_closing_resource(self):
        realm = object()
        resource = {"closed": False}
        seen = []

        @plugin
        def provider(ctx, config):
            ctx.isolate("db", realm=realm).provide("db", resource)

            def close():
                resource["closed"] = True
                seen.append("provider")

            return close

        @plugin(inject=["db"])
        def consumer(ctx, config):
            def close():
                seen.append(("consumer", ctx.db["closed"]))

            return close

        async with Context() as root:
            fiber = await root.use(provider)
            await root.isolate("db", realm=realm).use(consumer)
            await fiber.dispose()
            self.assertEqual(seen, [("consumer", False), "provider"])

    async def test_explicit_falsey_realms_are_shared(self):
        class FalseyRealm:
            def __bool__(self):
                return False

        for realm in (0, False, "", FalseyRealm()):
            with self.subTest(realm=realm):
                async with Context() as root:
                    left = root.isolate("db", realm=realm)
                    right = root.isolate("db", realm=realm)
                    left.provide("db", "ready")
                    self.assertEqual(right.get("db"), "ready")

    async def test_implicit_realms_remain_separate(self):
        async with Context() as root:
            left = root.isolate("db")
            right = root.isolate("db")
            left.provide("db", "ready")
            self.assertIsNone(right.get("db"))
