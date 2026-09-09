from __future__ import annotations

import asyncio
import json
import ssl
from collections.abc import Sequence
from http import HTTPStatus
from urllib.parse import urlsplit

from .contracts import Json


class DeepSeekAPI:
    def __init__(
        self,
        api_key: str,
        *,
        base_url: str,
        model: str,
        thinking: bool,
        timeout: float,
    ):
        self.api_key = api_key
        self.endpoint = base_url.rstrip("/") + "/chat/completions"
        self.model = model
        self.thinking = thinking
        self.timeout = timeout

    async def complete(self, messages: Sequence[Json], tools: Sequence[Json]) -> Json:
        payload: Json = {
            "model": self.model,
            "messages": list(messages),
            "tools": list(tools),
            "tool_choice": "auto",
            "thinking": {"type": "enabled" if self.thinking else "disabled"},
            "max_tokens": 4096,
        }
        try:
            async with asyncio.timeout(self.timeout):
                data = await self._request(payload)
        except TimeoutError as error:
            raise RuntimeError(f"DeepSeek API 请求超过 {self.timeout:g} 秒") from error
        except (OSError, asyncio.IncompleteReadError) as error:
            raise RuntimeError(f"无法连接 DeepSeek API：{error}") from error

        try:
            message = data["choices"][0]["message"]
        except (KeyError, IndexError, TypeError) as error:
            raise RuntimeError(f"DeepSeek API 响应格式异常：{data!r}") from error
        if not isinstance(message, dict) or message.get("role") != "assistant":
            raise RuntimeError(f"DeepSeek API 未返回 assistant message：{message!r}")
        return message

    async def _request(self, payload: Json) -> Json:
        url = urlsplit(self.endpoint)
        if url.scheme not in {"http", "https"} or not url.hostname:
            raise ValueError(f"不支持的 DeepSeek API URL：{self.endpoint!r}")

        secure = url.scheme == "https"
        port = url.port or (443 if secure else 80)
        ssl_context = (
            await asyncio.to_thread(ssl.create_default_context) if secure else None
        )
        reader, writer = await asyncio.open_connection(
            url.hostname,
            port,
            ssl=ssl_context,
            server_hostname=url.hostname if secure else None,
        )
        body = json.dumps(payload, ensure_ascii=False).encode()
        path = url.path or "/"
        if url.query:
            path += f"?{url.query}"
        host = url.hostname if url.port is None else f"{url.hostname}:{url.port}"
        headers = (
            f"POST {path} HTTP/1.1\r\n"
            f"Host: {host}\r\n"
            f"Authorization: Bearer {self.api_key}\r\n"
            "Content-Type: application/json\r\n"
            "Accept: application/json\r\n"
            "User-Agent: cordis-python-agent-demo/0.2\r\n"
            f"Content-Length: {len(body)}\r\n"
            "Connection: close\r\n\r\n"
        ).encode("ascii")

        try:
            writer.write(headers + body)
            await writer.drain()
            status, response_headers = await self._read_headers(reader)
            response_body = await self._read_body(reader, response_headers)
        finally:
            writer.close()
            await writer.wait_closed()

        text = response_body.decode("utf-8", errors="replace")
        if not 200 <= status < 300:
            try:
                reason = HTTPStatus(status).phrase
            except ValueError:
                reason = "Unknown Status"
            raise RuntimeError(f"DeepSeek API HTTP {status} {reason}: {text}")
        try:
            data = json.loads(text)
        except json.JSONDecodeError as error:
            raise RuntimeError(f"DeepSeek API 返回了无效 JSON：{text[:500]}") from error
        if not isinstance(data, dict):
            raise RuntimeError(f"DeepSeek API 响应不是 JSON object：{data!r}")
        return data

    @staticmethod
    async def _read_headers(
        reader: asyncio.StreamReader,
    ) -> tuple[int, dict[str, str]]:
        raw = await reader.readuntil(b"\r\n\r\n")
        lines = raw[:-4].decode("iso-8859-1").split("\r\n")
        try:
            version, status_text, _reason = lines[0].split(" ", 2)
            status = int(status_text)
        except (ValueError, IndexError) as error:
            raise RuntimeError(f"无效 HTTP 状态行：{lines[0]!r}") from error
        if not version.startswith("HTTP/"):
            raise RuntimeError(f"无效 HTTP 状态行：{lines[0]!r}")

        headers: dict[str, str] = {}
        for line in lines[1:]:
            name, separator, value = line.partition(":")
            if not separator:
                raise RuntimeError(f"无效 HTTP 响应头：{line!r}")
            headers[name.strip().lower()] = value.strip()
        return status, headers

    @staticmethod
    async def _read_body(
        reader: asyncio.StreamReader,
        headers: dict[str, str],
    ) -> bytes:
        if "chunked" in headers.get("transfer-encoding", "").lower():
            chunks: list[bytes] = []
            while True:
                size_line = await reader.readline()
                try:
                    size = int(size_line.split(b";", 1)[0].strip(), 16)
                except ValueError as error:
                    raise RuntimeError(f"无效 chunk size：{size_line!r}") from error
                if size == 0:
                    while await reader.readline() not in {b"\r\n", b""}:
                        pass
                    return b"".join(chunks)
                chunks.append(await reader.readexactly(size))
                if await reader.readexactly(2) != b"\r\n":
                    raise RuntimeError("无效 chunk 结尾")

        length = headers.get("content-length")
        if length is not None:
            try:
                size = int(length)
            except ValueError as error:
                raise RuntimeError(f"无效 Content-Length：{length!r}") from error
            return await reader.readexactly(size)
        return await reader.read()
