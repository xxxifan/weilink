"""Keep consecutive owner input together until the first reply starts sending."""

from __future__ import annotations

import asyncio
import contextlib

from appserver import RpcError
from generated_images import IMAGE_TYPES
from incoming_images import IncomingImageError
from incoming_videos import IncomingVideoError


class MessageBatch:
    def __init__(self, bridge, batch_id: str) -> None:
        self.bridge = bridge
        self.state = bridge.state
        self.id = batch_id
        self.thread_id = self.state.batch(batch_id)["thread_id"]
        self.current = None
        self.model_settings = bridge.turn_model_settings(self.thread_id)
        self.rollout_path = None
        self.rollout_offset = 0
        self.deadline = asyncio.get_running_loop().time() + bridge.config.get(
            "turn_timeout_seconds", 3600
        )

    async def inputs(self) -> tuple[list[dict], list[str]]:
        parts, ids = [], []
        while True:
            if asyncio.get_running_loop().time() >= self.deadline:
                raise TimeoutError("Consecutive message preparation timed out")
            messages = [
                job
                for job in self.state.batch_messages(self.id, pending=True)
                if job["message_id"] not in ids
            ]
            if not messages:
                return parts, ids
            self.state.batch_processing(
                self.id, [job["message_id"] for job in messages]
            )
            for job in messages:
                parts.extend(await self.bridge.prepare_message(job))
                ids.append(job["message_id"])
            # Media download/ASR can outlive another message arrival. Drain
            # those arrivals before sending the first model request as well.

    def jobs(self) -> list[dict]:
        return self.state.batch_messages(self.id)

    async def start(self, parts: list[dict], ids: list[str]) -> None:
        bridge = self.bridge
        await bridge.ensure_thread(self.thread_id)
        self.rollout_path = self.state.get(f"rollout:{self.thread_id}")
        self.rollout_offset = bridge.generated_images.checkpoint(self.rollout_path)
        self.current = {
            "batch_id": self.id,
            "thread_id": self.thread_id,
            "turn_id": None,
            "done": asyncio.get_running_loop().create_future(),
            "items": {},
            "model_settings": dict(self.model_settings),
            "input": list(parts),
            "input_ids": list(ids),
            "accepted": False,
            "voice_reply": any(job["has_voice"] for job in self.jobs()),
        }
        bridge.active = self.current
        response = await bridge.rpc.request(
            "turn/start",
            {
                "threadId": self.thread_id,
                "input": parts,
                **({"clientUserMessageId": ids[0]} if ids else {}),
                "turnTrigger": "wechat-message",
                **self.model_settings,
            },
        )
        self.current["turn_id"] = response["turn"]["id"]
        self.current["accepted"] = True
        self.state.batch_submitted(ids)

    async def append(self, parts: list[dict], ids: list[str]) -> None:
        # The earlier user input remains in the same thread's history. Do not
        # replay it or interrupt/restart executing tools to add a late image.
        parts.insert(
            0,
            {
                "type": "text",
                "text": "主人在本批回复发送前继续补充了以下消息。请把它们与本批前面的消息一起理解，重新形成一份完整答复；前面的草稿尚未发送，不要重复执行已经完成的操作。",
            },
        )
        if self.current is not None and not self.current["done"].done():
            try:
                response = await self.bridge.rpc.request(
                    "turn/steer",
                    {
                        "threadId": self.thread_id,
                        "expectedTurnId": self.current["turn_id"],
                        "input": parts,
                    },
                )
            except RpcError as exc:
                # Only an explicit completed-turn rejection is safe to retry
                # as a continuation. Unknown/transport failures may already
                # have accepted input and must never be blindly replayed.
                closed = any(
                    word in str(exc).lower()
                    for word in (
                        "no active turn",
                        "expected turn",
                        "not the active turn",
                    )
                )
                if not closed:
                    raise
                await asyncio.wait_for(
                    asyncio.shield(self.current["done"]),
                    max(0, self.deadline - asyncio.get_running_loop().time()),
                )
            else:
                if response.get("turnId") != self.current["turn_id"]:
                    raise RpcError("Steer response did not match the active turn")
                self.current["steered"] = True
                self.current["voice_reply"] = any(
                    job["has_voice"] for job in self.jobs()
                )
                self.state.batch_submitted(ids)
                return
        if self.current is not None and self.current["done"].done():
            turn = self.current["done"].result()
            if turn.get("status") == "failed":
                # Leave these inputs pending for a user-authorized continuation.
                return
        await self.start(parts, ids)

    async def settle(self) -> None:
        """Confirm the previous attempt stopped before accepting another turn."""
        current = self.current
        if not self.bridge.connected:
            # Restart only after the user selects recovery. Closing the old
            # process prevents its uncertain request from running in parallel.
            await self.bridge.rpc.close()
            await self.bridge.rpc.start()
            self.bridge.connected = True
            self.bridge.resumed.clear()
        if current is None:
            return
        if current["done"].done() and not current["done"].exception():
            return
        if current.get("turn_id") and not current["done"].done():
            await self.bridge.rpc.request(
                "turn/interrupt",
                {
                    "threadId": self.thread_id,
                    "turnId": current["turn_id"],
                },
            )
            await asyncio.wait_for(asyncio.shield(current["done"]), 10)
            return
        # An RPC timeout/disconnect does not prove that turn/start was rejected.
        # Read saved turns instead of blindly submitting the same message again.
        response = await self.bridge.rpc.request(
            "thread/read",
            {
                "threadId": self.thread_id,
                "includeTurns": True,
            },
        )
        turns = response.get("thread", {}).get("turns")
        if not isinstance(turns, list):
            raise RpcError("无法确认上次请求状态，请稍后重试或停止本次请求")
        running = [turn for turn in turns if turn.get("status") == "inProgress"]
        if running:
            turn_id = running[-1]["id"]
            await self.bridge.rpc.request(
                "turn/interrupt",
                {
                    "threadId": self.thread_id,
                    "turnId": turn_id,
                },
            )
            response = await self.bridge.rpc.request(
                "thread/read",
                {
                    "threadId": self.thread_id,
                    "includeTurns": True,
                },
            )
            turns = response.get("thread", {}).get("turns")
            if not isinstance(turns, list) or any(
                turn.get("status") == "inProgress" for turn in turns
            ):
                raise RpcError("上次请求尚未确认停止，请稍后重试")
        for turn in turns:
            if turn.get("id") == current.get("turn_id") and turn.get("status") in {
                "completed",
                "failed",
                "interrupted",
            }:
                # Rehydrate a known completed attempt after reconnecting, so a
                # successful result is not executed again just because RPC died.
                current["done"] = asyncio.get_running_loop().create_future()
                current["done"].set_result(turn)
                for item in turn.get("items", []):
                    if item.get("type") in {"agentMessage", *IMAGE_TYPES}:
                        current["items"][item["id"]] = item
                break

    async def restart(self, choice: dict) -> None:
        await self.settle()
        if self.current and self.current.get("stop_requested"):
            return
        if (
            self.current
            and self.current["done"].done()
            and not self.current["done"].exception()
        ):
            if self.current["done"].result().get("status") == "completed":
                # A local RPC wait may time out while Codex still completes.
                # Publish that result instead of executing the user's work twice.
                self.state.batch_submitted(self.current["input_ids"])
                self.deadline = (
                    asyncio.get_running_loop().time()
                    + self.bridge.config.get("turn_timeout_seconds", 3600)
                )
                return
        self.model_settings.update(choice)
        self.bridge.requests.clear()
        self.deadline = asyncio.get_running_loop().time() + self.bridge.config.get(
            "turn_timeout_seconds", 3600
        )
        if self.current and self.current["accepted"]:
            self.state.batch_submitted(self.current["input_ids"])
        parts, ids = await self.inputs()
        if self.current is not None:
            # The user explicitly requested recovery. Unknown submissions are
            # included as context once, without replaying their external IDs.
            if not self.current["accepted"]:
                parts = list(self.current["input"]) + [
                    part for part in parts if part not in self.current["input"]
                ]
                original_ids = set(self.current["input_ids"])
                self.state.batch_submitted(list(original_ids))
                ids = [
                    message_id for message_id in ids if message_id not in original_ids
                ]
            parts.insert(
                0,
                {
                    "type": "text",
                    "text": "主人已通过 WeiLink 命令明确选择重试或更换模型。请先核对当前会话历史及已经完成的操作，只继续尚未完成的请求，并结合以下原请求上下文或补充消息形成完整答复；不要重复执行已完成的操作，不要把失败草稿当作已发送答复。",
                },
            )
        await self.start(parts, ids)
        if choice:
            self.state.put(f"model_settings:{self.thread_id}", self.model_settings)

    def stopped_result(self, unconfirmed: bool = False) -> dict:
        # Cancel pending supplements with this batch; they must not reopen it
        # or be silently executed after the user selected /stop.
        self.state.batch_submitted(
            [
                job["message_id"]
                for job in self.state.batch_messages(self.id, pending=True)
            ]
        )
        return {
            "turn": {
                "id": (self.current or {}).get("turn_id") or self.id,
                "status": "interrupted",
            },
            "items": [
                {
                    "type": "agentMessage",
                    "id": f"cancel:{self.id}",
                    "text": "已取消本次恢复，不再重试或换模型；上次请求的停止状态未能确认。",
                }
            ]
            if unconfirmed
            else [],
            "thread_id": self.thread_id,
            "rollout_path": self.rollout_path,
            "rollout_offset": self.rollout_offset,
            "revision": self.state.batch(self.id)["revision"],
        }

    async def run(self) -> dict:
        while True:
            try:
                result = await self.run_attempt()
            except (IncomingImageError, IncomingVideoError):
                raise
            except Exception as exc:
                error = exc
            else:
                if result["turn"].get("status") != "failed":
                    return result
                error = result["turn"].get("error") or {"message": "Codex 返回失败状态"}
            while True:
                choice = await self.bridge.request_recovery(self, error)
                if choice is None:
                    settled = self.current is None or (
                        self.current["done"].done()
                        and not self.current["done"].exception()
                    )
                    if self.bridge.connected and not settled:
                        with contextlib.suppress(Exception):
                            await self.settle()
                            settled = True
                    return self.stopped_result(unconfirmed=not settled)
                try:
                    await self.restart(choice)
                except (IncomingImageError, IncomingVideoError):
                    raise
                except Exception as exc:
                    error = exc
                    continue
                break

    async def run_attempt(self) -> dict:
        while True:
            remaining = self.deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise TimeoutError("Consecutive message batch timed out")
            if self.bridge.stopping.is_set():
                raise RuntimeError("Bridge is stopping")
            stopping = self.current is not None and self.current.get("stop_requested")
            if stopping:
                await asyncio.wait_for(asyncio.shield(self.current["done"]), remaining)
                self.state.batch_submitted(
                    [
                        job["message_id"]
                        for job in self.state.batch_messages(self.id, pending=True)
                    ]
                )
            failed = (
                self.current is not None
                and self.current["done"].done()
                and self.current["done"].result().get("status") == "failed"
            )
            parts, ids = ([], []) if stopping or failed else await self.inputs()
            if ids:
                if self.current is None and not any(
                    job["submitted"] for job in self.jobs()
                ):
                    await self.start(parts, ids)
                else:
                    await self.append(parts, ids)
                continue
            if self.state.batch_routing(self.id):
                await asyncio.sleep(0.05)
                continue
            if self.current is None:
                raise RuntimeError("Message batch has no pending model input")
            if self.current["done"].done():
                turn = self.current["done"].result()
                return {
                    "turn": turn,
                    "items": list(self.current["items"].values()),
                    "thread_id": self.thread_id,
                    "rollout_path": self.rollout_path,
                    "rollout_offset": self.rollout_offset,
                    "revision": self.state.batch(self.id)["revision"],
                }
            try:
                await asyncio.wait_for(
                    asyncio.shield(self.current["done"]), min(0.1, remaining)
                )
            except TimeoutError:
                pass
