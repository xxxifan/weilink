"""A small JSONL client for the installed Codex App Server."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import Awaitable, Callable
from typing import Any

log = logging.getLogger(__name__)


class RpcError(RuntimeError):
    pass


class AppServer:
    def __init__(
        self,
        command: list[str],
        on_event: Callable[[dict], Awaitable[None]],
        on_request: Callable[[dict], Awaitable[None]],
    ) -> None:
        self.command = command
        self.on_event = on_event
        self.on_request = on_request
        self.process: asyncio.subprocess.Process | None = None
        self.pending: dict[int, asyncio.Future] = {}
        self.sequence = 0
        self.reader_task: asyncio.Task | None = None
        self.stderr_task: asyncio.Task | None = None
        self.write_lock = asyncio.Lock()

    async def start(self) -> None:
        self.process = await asyncio.create_subprocess_exec(
            *self.command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={**os.environ, "PYTHONUTF8": "1"},
            limit=16 * 1024 * 1024,
        )
        self.reader_task = asyncio.create_task(self._read())
        self.stderr_task = asyncio.create_task(self._drain_stderr())
        await self.request(
            "initialize",
            {
                "clientInfo": {
                    "name": "wechat_codex_bridge",
                    "title": "WeChat Codex Bridge",
                    "version": "1.0.0",
                },
            },
        )
        await self.write({"method": "initialized", "params": {}})

    async def write(self, message: dict) -> None:
        if self.process is None or self.process.returncode is not None:
            raise RpcError("Codex App Server is not running")
        assert self.process.stdin is not None
        payload = json.dumps(message, ensure_ascii=False).encode("utf-8") + b"\n"
        async with self.write_lock:
            self.process.stdin.write(payload)
            await self.process.stdin.drain()

    async def request(self, method: str, params: dict, timeout: float = 120) -> Any:
        self.sequence += 1
        request_id = self.sequence
        future = asyncio.get_running_loop().create_future()
        self.pending[request_id] = future
        try:
            await self.write({"id": request_id, "method": method, "params": params})
            return await asyncio.wait_for(future, timeout)
        finally:
            self.pending.pop(request_id, None)

    async def respond(self, request_id: int | str, result: dict) -> None:
        await self.write({"id": request_id, "result": result})

    async def reject(self, request_id: int | str, message: str) -> None:
        await self.write(
            {"id": request_id, "error": {"code": -32601, "message": message}}
        )

    async def _read(self) -> None:
        assert self.process is not None and self.process.stdout is not None
        try:
            async for line in self.process.stdout:
                message = json.loads(line)
                if "id" in message and "method" not in message:
                    future = self.pending.get(message["id"])
                    if future is None or future.done():
                        continue
                    if "error" in message:
                        future.set_exception(RpcError(str(message["error"])))
                    else:
                        future.set_result(message.get("result"))
                elif "id" in message:
                    # Approval callbacks must not block the reader: their replies
                    # arrive later through another WeChat message.
                    asyncio.create_task(self.on_request(message))
                elif "method" in message:
                    await self.on_event(message)
        except Exception as exc:
            log.error("App Server reader failed: %s", type(exc).__name__)
        finally:
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(RpcError("Codex App Server disconnected"))
            await self.on_event({"method": "bridge/disconnected", "params": {}})

    async def _drain_stderr(self) -> None:
        assert self.process is not None and self.process.stderr is not None
        # Drain without saving raw stderr; provider diagnostics can contain
        # request metadata that should not be copied into bridge logs.
        async for _line in self.process.stderr:
            pass

    async def close(self) -> None:
        if self.process is None:
            return
        if self.process.stdin is not None:
            self.process.stdin.close()
        try:
            await asyncio.wait_for(self.process.wait(), 5)
        except TimeoutError:
            self.process.terminate()
            try:
                await asyncio.wait_for(self.process.wait(), 5)
            except TimeoutError:
                self.process.kill()
                await self.process.wait()
        for task in (self.reader_task, self.stderr_task):
            if task is not None and not task.done():
                task.cancel()
