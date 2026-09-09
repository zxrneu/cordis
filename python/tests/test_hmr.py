import importlib
import sys
import tempfile
import unittest
from pathlib import Path

from cordis import Context, Entry, FiberState, FullRestartRequired, Loader


def source(version, *, fail=False):
    body = (
        "raise RuntimeError('new plugin failed')"
        if fail
        else f"ctx.provide('version', {version!r})"
    )
    return f"""from cordis import plugin

@plugin(provide=["version"])
def app(ctx, _config):
    {body}
"""


class HMRTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        self.module = "cordis_hmr_fixture"
        self.path = self.directory / f"{self.module}.py"
        sys.path.insert(0, str(self.directory))
        self.path.write_text(source("v1"), encoding="utf-8")
        importlib.invalidate_caches()

    async def asyncTearDown(self):
        sys.modules.pop(self.module, None)
        sys.modules.pop("cordis_hmr_helper", None)
        sys.path.remove(str(self.directory))
        self.temp.cleanup()

    async def test_changed_module_reloads_its_entry(self):
        root = Context()
        loader = Loader(root)
        await loader.reconcile([Entry("app", f"{self.module}:app")])
        self.assertEqual(root.get("version"), "v1")

        self.path.write_text(source("version-two"), encoding="utf-8")
        reloaded = await loader.reload_changed([self.path])
        self.assertEqual(reloaded, ("app",))
        self.assertEqual(root.get("version"), "version-two")

    async def test_failed_component_activation_rolls_back(self):
        root = Context()
        loader = Loader(root)
        await loader.reconcile([Entry("app", f"{self.module}:app")])
        old_fiber = loader.get("app")

        self.path.write_text(source("v2", fail=True), encoding="utf-8")
        with self.assertRaisesRegex(ExceptionGroup, "hot reload failed"):
            await loader.reload_changed([self.path])

        self.assertEqual(root.get("version"), "v1")
        self.assertIsNot(loader.get("app"), old_fiber)
        self.assertEqual(loader.get("app").state, FiberState.ACTIVE)

    async def test_external_change_requests_a_full_restart(self):
        root = Context()
        loader = Loader(root)
        await loader.reconcile([Entry("app", f"{self.module}:app")])
        with self.assertRaises(FullRestartRequired):
            await loader.reload_changed([self.path], externals=[self.module])

    async def test_transitive_dependency_change_reloads_entry(self):
        helper = self.directory / "cordis_hmr_helper.py"
        helper.write_text("VERSION = 'one'\n", encoding="utf-8")
        self.path.write_text(
            """from cordis import plugin
from cordis_hmr_helper import VERSION

@plugin(provide=["version"])
def app(ctx, _config):
    ctx.provide("version", VERSION)
""",
            encoding="utf-8",
        )
        root = Context()
        loader = Loader(root)
        await loader.reconcile([Entry("app", f"{self.module}:app")])
        self.assertEqual(root.get("version"), "one")

        helper.write_text("VERSION = 'version-two'\n", encoding="utf-8")
        self.assertEqual(await loader.reload_changed([helper]), ("app",))
        self.assertEqual(root.get("version"), "version-two")
