"""Historical mistakes now rejected by the project's mypy configuration.

From python/:
  .venv/bin/python -m mypy docs/repros/any_audit_20260911.py
  PYTHONPATH=src .venv/bin/python docs/repros/any_audit_20260911.py

There are deliberately no Any annotations, casts, or type ignores in this file.
After the fix, mypy must reject all three SHOULD_REJECT lines. Running this
file bypasses type checking and still demonstrates the original runtime errors.
"""

import asyncio

from cordis import Context, Entry, ServiceKey


COUNT = ServiceKey[int]("count")


def fiber_escape(ctx: Context) -> int:
    ctx.fiber.ctx.provide(COUNT, "wrong")  # SHOULD_REJECT: int key, str value.
    value: int = ctx[COUNT]
    return value


def metadata_escape(entry: Entry) -> int:
    value: int = entry.intercept["db"].bit_length()  # SHOULD_REJECT: unknown type.
    return value


def event_escape(ctx: Context) -> int:
    value: int = ctx.bail("answer").bit_length()  # SHOULD_REJECT: unknown type.
    return value


def app(ctx: Context, config: None) -> None:
    pass


async def main() -> None:
    async with Context() as root:
        value = fiber_escape(root)
        print(f"T01: annotated int, actual {type(value).__name__}: {value!r}")

        root.on("answer", lambda: "text")
        try:
            event_escape(root)
        except AttributeError as error:
            print(f"T02: {type(error).__name__}: {error}")

        entry = Entry("app", app, intercept={"db": "text"})
        try:
            metadata_escape(entry)
        except AttributeError as error:
            print(f"T03: {type(error).__name__}: {error}")


if __name__ == "__main__":
    asyncio.run(main())
