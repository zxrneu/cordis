"""Run with: uv run python -m examples.typed_interfaces."""

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass

from cordis import Context, EventKey, Loader, QueryKey, plugin


@dataclass(frozen=True)
class Message:
    text: str


@dataclass(frozen=True)
class User:
    id: int
    name: str


@dataclass(frozen=True)
class WorkerConfig:
    prefix: str


class AppContext(Context):
    """Retain the context subclass for compatibility; new code can use Context."""

    pass


# Import and share these objects between producers and consumers. Their names
# are diagnostic labels; another key with the same name is a different channel.
MESSAGE = EventKey[Message]("message")
FIND_USER = QueryKey[int, User]("find_user")


def decode_config(raw: object) -> WorkerConfig:
    if not isinstance(raw, Mapping):
        raise ValueError("worker configuration must be a mapping")
    prefix = raw.get("prefix")
    if not isinstance(prefix, str):
        raise ValueError("prefix must be a string")
    return WorkerConfig(prefix)


@plugin
def worker(ctx: Context, config: WorkerConfig) -> None:
    def receive(message: Message) -> None:
        print(f"{config.prefix}: {message.text}")

    async def find_user(user_id: int) -> User | None:
        return User(user_id, "Cordis") if user_id == 1 else None

    ctx.on(MESSAGE, receive)
    ctx.handle(FIND_USER, find_user)


async def main() -> None:
    async with Context() as root:
        loader = Loader(root)
        fiber = await loader.mount("worker", worker.configure(WorkerConfig("hello")))
        view = loader.get("worker")
        if view is not None:
            await view.wait()
            print(f"worker state: {view.state.value}")
            # view.update(...) is intentionally absent from the static interface.

        root.emit(MESSAGE, Message("typed notification"))
        user = await root.query(FIND_USER, 1)
        if user is not None:
            print(f"query result: {user.name}")

        # The original mounting handle retains WorkerConfig, unlike the view.
        await fiber.update(WorkerConfig("updated"))
        await root.parallel(MESSAGE, Message("typed update"))
        await loader.unmount("worker")
        assert await root.query(FIND_USER, 1) is None

        # External data is decoded before the plugin/config pair is bound.
        binding = await worker.decode({"prefix": "decoded"}, decode_config)
        await loader.mount("worker", binding)
        root.emit(MESSAGE, Message("configuration boundary"))


if __name__ == "__main__":
    asyncio.run(main())
