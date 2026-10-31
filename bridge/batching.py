"""Keep consecutive owner input together until the first reply starts sending."""

from __future__ import annotations

import asyncio

from appserver import RpcError


class MessageBatch:
    def __init__(self, bridge, batch_id: str) -> None:
        self.bridge = bridge
        self.state = bridge.state
        self.id = batch_id
        self.thread_id = self.state.batch(batch_id)["thread_id"]
        self.current = None
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
            "voice_reply": any(job["has_voice"] for job in self.jobs()),
        }
        bridge.active = self.current
        response = await bridge.rpc.request(
            "turn/start",
            {
                "threadId": self.thread_id,
                "input": parts,
                "clientUserMessageId": ids[0],
                "turnTrigger": "wechat-message",
                **bridge.turn_model_settings(),
            },
        )
        self.current["turn_id"] = response["turn"]["id"]
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
        await self.start(parts, ids)

    async def run(self) -> dict:
        while True:
            remaining = self.deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise TimeoutError("Consecutive message batch timed out")
            stopping = self.current is not None and self.current.get("stop_requested")
            if stopping:
                await asyncio.wait_for(asyncio.shield(self.current["done"]), remaining)
                self.state.batch_submitted(
                    [
                        job["message_id"]
                        for job in self.state.batch_messages(self.id, pending=True)
                    ]
                )
            parts, ids = ([], []) if stopping else await self.inputs()
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
