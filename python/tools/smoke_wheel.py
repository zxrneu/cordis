"""Run with an installed wheel: python -I tools/smoke_wheel.py."""

import asyncio
from importlib.metadata import distribution
from pathlib import Path

import cordis
from cordis import Context, ServiceKey, plugin


VALUE = ServiceKey[int]("value")


async def main() -> None:
    package = distribution("cordis-python")
    assert Path(cordis.__file__).resolve() == Path(str(
        package.locate_file("cordis/__init__.py")
    )).resolve(), "smoke test must import the installed wheel"
    assert Path(str(package.locate_file("cordis/py.typed"))).is_file()
    for name in cordis.__all__:
        assert getattr(cordis, name) is not None, name

    seen: list[int] = []

    @plugin(provide=[VALUE])
    def provider(ctx: Context, value: int) -> None:
        ctx.provide(VALUE, value)

    @plugin(inject=[VALUE])
    def consumer(ctx: Context, _config: None) -> None:
        seen.append(ctx.service(VALUE))

    async with Context(max_tasks=8, error_history_limit=4) as root:
        root.use(consumer)
        writer = root.use(provider, 1)
        await root.wait_ready(timeout=5)
        await writer.update(2)
        await root.wait_ready(timeout=5)
        assert seen == [1, 2], seen
        assert await root.spawn(asyncio.sleep(0, result=42)) == 42
        assert root.task_count == 0
    assert root.fibers == ()
    assert root.errors == ()
    print(f"cordis-python {package.version}: installed wheel smoke test passed")


if __name__ == "__main__":
    asyncio.run(main())
