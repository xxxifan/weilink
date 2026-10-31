from __future__ import annotations

import asyncio
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock

from weilink.models import Message, MessageType

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from appserver import RpcError
from state import State
from wechat_bridge import Bridge


class BatchStateTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "state.db"
        self.state = State(self.path)

    def tearDown(self):
        self.state.close()
        self.directory.cleanup()

    def queue(self, message_id, thread="thread"):
        self.state.accept(message_id, "text", thread)
        return self.state.queue_message(message_id)

    def ready(self, batch_id):
        ids = [job["message_id"] for job in self.state.batch_messages(batch_id)]
        self.state.batch_processing(batch_id, ids)
        self.state.batch_submitted(ids)
        self.assertTrue(
            self.state.complete(
                batch_id,
                ["old answer"],
                image_paths=["old.png"],
                voice_paths=["old.mp3"],
                batch_id=batch_id,
                batch_revision=self.state.batch(batch_id)["revision"],
            )
        )

    def test_queued_messages_join_only_their_own_conversation(self):
        first = self.queue("one")
        self.assertEqual(self.queue("two"), first)
        self.assertNotEqual(self.queue("other", "other-thread"), first)
        self.assertEqual(
            [row["message_id"] for row in self.state.batch_messages(first)],
            ["one", "two"],
        )

    def test_unsent_ready_result_is_superseded_including_all_media(self):
        batch = self.queue("one")
        self.ready(batch)
        self.assertEqual(self.queue("two"), batch)
        self.assertIsNone(self.state.next_reply())
        self.assertEqual(
            [row[0] for row in self.state.db.execute("SELECT status FROM outbox")],
            ["suppressed"] * 3,
        )
        self.state.batch_processing(batch, ["two"])
        self.state.batch_submitted(["two"])
        self.assertTrue(
            self.state.complete(batch, ["new answer"], batch_id=batch, batch_revision=1)
        )
        self.assertEqual(self.state.next_reply()["text"], "new answer")

    def test_first_network_send_seals_the_entire_reply_group(self):
        batch = self.queue("one")
        self.ready(batch)
        reply = self.state.next_reply()
        self.assertTrue(self.state.begin_reply(reply["id"]))
        self.assertFalse(self.state.begin_reply(reply["id"]))
        self.assertNotEqual(self.queue("two"), batch)
        self.assertEqual(self.state.batch(batch)["status"], "sealed")

    def test_accepted_but_still_routing_message_blocks_publication_and_send(self):
        batch = self.queue("one")
        self.ready(batch)
        self.state.accept("voice", "", "thread", has_voice=True)
        self.state.mark("voice", "transcribing")
        self.assertFalse(self.state.begin_reply(self.state.next_reply()["id"]))
        self.assertFalse(
            self.state.complete(batch, ["stale"], batch_id=batch, batch_revision=0)
        )
        self.state.mark("voice", "answer")
        self.assertTrue(self.state.begin_reply(self.state.next_reply()["id"]))

    def test_revision_prevents_draft_publication_during_slow_tts(self):
        batch = self.queue("one")
        self.state.batch_processing(batch, ["one"])
        self.state.batch_submitted(["one"])
        self.queue("two")
        self.assertFalse(
            self.state.complete(batch, ["stale"], batch_id=batch, batch_revision=0)
        )
        self.assertIsNone(self.state.next_reply())

    def test_queued_batch_survives_restart_without_losing_members(self):
        batch = self.queue("one")
        self.queue("two")
        self.state.close()
        self.state = State(self.path)
        self.assertEqual(self.state.recover(), 0)
        self.assertEqual(len(self.state.batch_messages(batch, pending=True)), 2)
        self.assertEqual(self.state.next_message()["message_id"], "one")

    def test_crash_never_replays_late_input_from_an_executing_batch(self):
        batch = self.queue("one")
        self.state.batch_processing(batch, ["one"])
        self.state.batch_submitted(["one"])
        self.queue("two")
        self.assertEqual(self.state.recover(), 2)
        self.assertIsNone(self.state.next_message())
        self.assertTrue(
            all(
                row["status"] == "interrupted"
                for row in self.state.batch_messages(batch)
            )
        )
        self.assertNotEqual(self.queue("three"), batch)


class ControlledRpc:
    def __init__(self):
        self.bridge = None
        self.calls = []
        self.responses = []
        self.turn = 0
        self.thread = 0
        self.steer_failure = None

    async def request(self, method, params):
        self.calls.append((method, params))
        if method == "thread/start":
            self.thread += 1
            return {"thread": {"id": f"thread-{self.thread}"}}
        if method == "turn/start":
            self.turn += 1
            turn_id = f"turn-{self.turn}"
            await self.bridge.on_event(
                {
                    "method": "turn/started",
                    "params": {
                        "threadId": params["threadId"],
                        "turn": {"id": turn_id},
                    },
                }
            )
            return {"turn": {"id": turn_id}}
        if method == "turn/steer":
            if self.steer_failure == "closed":
                await self.finish("过时的追问")
                raise RpcError("no active turn")
            if self.steer_failure:
                raise RpcError("transport result unknown")
            return {"turnId": params["expectedTurnId"]}
        if method == "turn/interrupt":
            await self.finish("", status="interrupted")
        return {}

    async def respond(self, request_id, result):
        self.responses.append((request_id, result))

    async def reject(self, request_id, message):
        self.responses.append((request_id, message))

    async def finish(
        self, text="结合图片后的完整答复", status="completed", turn_id=None
    ):
        active = self.bridge.active
        await self.bridge.on_event(
            {
                "method": "turn/completed",
                "params": {
                    "threadId": active["thread_id"],
                    "turn": {
                        "id": turn_id or active["turn_id"],
                        "status": status,
                        "items": [
                            {
                                "type": "agentMessage",
                                "id": f"answer-{self.turn}",
                                "phase": "final_answer",
                                "text": text,
                            }
                        ],
                    },
                },
            }
        )


class BatchBridgeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.state = State(self.root / "state.db")
        self.rpc = ControlledRpc()
        self.images = Mock()
        self.images.prepare.side_effect = lambda message_id, _cache: str(
            self.root / f"{message_id}.png"
        )
        self.voices = Mock()
        self.voices.incoming.return_value = (
            str(self.root / "voice.audio"),
            "这是语音补充",
        )
        self.voices.outgoing.return_value = ([str(self.root / "reply.silk")], True)
        self.generated = Mock()
        self.generated.checkpoint.return_value = 0
        self.generated.collect.return_value = ([], False)
        self.sent = []
        self.bridge = Bridge(
            {
                "owner_user_id": "owner",
                "workspace": self.directory.name,
                "default_persona": "off",
            },
            self.state,
            rpc=self.rpc,
            incoming_images=self.images,
            voices=self.voices,
            generated_images=self.generated,
            sender=lambda _config, text: self.sent.append(text) or True,
            voice_sender=lambda *_: True,
        )
        self.rpc.bridge = self.bridge
        self.bridge.connected = True
        self.tasks = []

    async def asyncTearDown(self):
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        self.state.close()
        self.directory.cleanup()

    def worker(self, sending=False):
        task = asyncio.create_task(
            self.bridge.outgoing_worker() if sending else self.bridge.model_worker()
        )
        self.tasks.append(task)
        return task

    async def until(self, predicate):
        async with asyncio.timeout(4):
            while not predicate():
                await asyncio.sleep(0.01)

    def calls(self, method):
        return [params for name, params in self.rpc.calls if name == method]

    async def receive(self, message_id, text=None, media=MessageType.TEXT):
        await self.bridge.receive(
            Message(from_user="owner", message_id=message_id, text=text, msg_type=media)
        )

    async def ready(self):
        await self.until(lambda: self.state.next_reply() is not None)

    async def test_queued_text_then_image_have_one_model_input_and_one_reply(self):
        await self.receive(1, "这是")
        await self.receive(2, media=MessageType.IMAGE)
        self.worker()
        await self.until(lambda: self.calls("turn/start"))
        parts = self.calls("turn/start")[0]["input"]
        self.assertEqual(
            [part["type"] for part in parts], ["text", "text", "localImage"]
        )
        self.assertIn("这是", parts[0]["text"])
        await self.rpc.finish()
        await self.ready()
        self.worker(sending=True)
        await self.until(lambda: len(self.sent) == 1)
        self.assertEqual(self.sent, ["结合图片后的完整答复"])
        self.assertEqual(
            [row[0] for row in self.state.db.execute("SELECT status FROM inbox")],
            ["done", "done"],
        )

    async def test_recovered_routing_row_without_a_session_is_bound_before_batching(
        self,
    ):
        self.state.accept("1", "恢复的文字", None)
        self.state.recover()
        self.worker()
        await self.until(lambda: self.calls("turn/start"))
        self.assertEqual(
            self.state.batch("1")["thread_id"], self.calls("turn/start")[0]["threadId"]
        )
        await self.rpc.finish("已恢复")
        await self.ready()
        self.assertEqual(self.state.next_reply()["text"], "已恢复")

    async def test_image_arriving_while_thinking_steers_without_interrupting(self):
        await self.receive(1, "这是")
        self.worker()
        await self.until(lambda: self.calls("turn/start"))
        await self.receive(2, media=MessageType.IMAGE)
        await self.until(lambda: self.calls("turn/steer"))
        self.assertIsNone(self.state.next_reply())
        self.assertEqual(self.calls("turn/steer")[0]["expectedTurnId"], "turn-1")
        self.assertEqual(self.calls("turn/steer")[0]["input"][-1]["type"], "localImage")
        self.assertFalse(self.calls("turn/interrupt"))
        self.assertEqual(len(self.calls("turn/start")), 1)
        await self.rpc.finish()
        await self.ready()
        self.assertEqual(self.state.next_reply()["text"], "结合图片后的完整答复")

    async def test_late_image_download_holds_a_completed_draft_then_continues(self):
        entered, release = threading.Event(), threading.Event()

        def prepare(*_args):
            entered.set()
            if not release.wait(3):
                raise TimeoutError()
            return str(self.root / "image.png")

        self.images.prepare.side_effect = prepare
        await self.receive(1, "这是")
        self.worker()
        await self.until(lambda: self.calls("turn/start"))
        await self.receive(2, media=MessageType.IMAGE)
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait, 2))
            await self.rpc.finish("你说的是什么？")
            await asyncio.sleep(0.1)
            self.assertIsNone(self.state.next_reply())
        finally:
            release.set()
        await self.until(lambda: len(self.calls("turn/start")) == 2)
        await self.rpc.finish("迟到的旧事件", turn_id="turn-1")
        self.assertIsNone(self.state.next_reply())
        await self.rpc.finish()
        await self.ready()
        self.assertEqual(self.state.next_reply()["text"], "结合图片后的完整答复")

    async def test_ready_but_unsent_answer_is_replaced_when_next_message_arrives(self):
        await self.receive(1, "这是")
        self.worker()
        await self.until(lambda: self.calls("turn/start"))
        await self.rpc.finish("你说的是什么？")
        await self.ready()
        await self.receive(2, media=MessageType.IMAGE)
        self.assertIsNone(self.state.next_reply())
        await self.until(lambda: len(self.calls("turn/start")) == 2)
        self.assertIn("草稿尚未发送", self.calls("turn/start")[1]["input"][0]["text"])
        await self.rpc.finish()
        await self.ready()
        self.worker(sending=True)
        await self.until(lambda: self.sent)
        self.assertEqual(self.sent, ["结合图片后的完整答复"])

    async def test_new_message_during_tts_discards_both_stale_audio_and_text(self):
        entered, release = threading.Event(), threading.Event()

        def outgoing(_text):
            entered.set()
            if not release.wait(3):
                raise TimeoutError()
            return [str(self.root / "stale.mp3")], True

        self.voices.outgoing.side_effect = outgoing
        await self.receive(1, "这是")
        self.worker()
        await self.until(lambda: self.calls("turn/start"))
        await self.rpc.finish("[[reply:voice]]\n你说的是什么？")
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait, 2))
            await self.receive(2, "用文字回复这张图", MessageType.IMAGE)
            self.assertIsNone(self.state.next_reply())
        finally:
            release.set()
        await self.until(lambda: len(self.calls("turn/start")) == 2)
        await self.rpc.finish()
        await self.ready()
        rows = self.state.db.execute(
            "SELECT kind,text FROM outbox WHERE status='pending'"
        ).fetchall()
        self.assertEqual([row[0] for row in rows], ["text"])
        self.assertEqual(rows[0][1], "结合图片后的完整答复")

    async def test_message_after_send_begins_forms_a_new_batch(self):
        entered, release = threading.Event(), threading.Event()

        def send(_config, text):
            entered.set()
            if not release.wait(3):
                raise TimeoutError()
            self.sent.append(text)
            return True

        self.bridge.sender = send
        await self.receive(1, "第一条")
        self.worker()
        await self.until(lambda: self.calls("turn/start"))
        await self.rpc.finish("第一轮答复")
        await self.ready()
        self.worker(sending=True)
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait, 2))
            await self.receive(2, "第二条")
            self.assertEqual(self.state.batch("1")["status"], "sealed")
            self.assertEqual(self.state.next_message()["batch_id"], "2")
        finally:
            release.set()
        await self.until(lambda: len(self.calls("turn/start")) == 2)
        await self.rpc.finish("第二轮答复")
        await self.until(lambda: len(self.sent) == 2)
        self.assertEqual(self.sent, ["第一轮答复", "第二轮答复"])

    async def test_three_consecutive_messages_and_duplicate_are_applied_once(self):
        await self.receive(1, "这是")
        self.worker()
        await self.until(lambda: self.calls("turn/start"))
        await self.receive(2, media=MessageType.IMAGE)
        await self.until(lambda: len(self.calls("turn/steer")) == 1)
        await self.receive(2, media=MessageType.IMAGE)
        await self.receive(3, "解释一下红色的地方")
        await self.until(lambda: len(self.calls("turn/steer")) == 2)
        await self.rpc.finish()
        await self.ready()
        self.images.prepare.assert_called_once()
        self.assertEqual(len(self.calls("turn/start")), 1)
        self.assertEqual(
            self.state.db.execute("SELECT count(*) FROM outbox").fetchone()[0], 1
        )

    async def test_voice_asr_already_received_holds_a_ready_answer(self):
        entered, release = threading.Event(), threading.Event()

        def incoming(_id):
            entered.set()
            if not release.wait(3):
                raise TimeoutError()
            return str(self.root / "voice.audio"), "请用文字回复这张图"

        self.voices.incoming.side_effect = incoming
        await self.receive(1, "这是")
        self.worker()
        await self.until(lambda: self.calls("turn/start"))
        await self.rpc.finish("你说的是什么？")
        await self.ready()
        incoming_task = asyncio.create_task(self.receive(2, media=MessageType.VOICE))
        self.tasks.append(incoming_task)
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait, 2))
            self.worker(sending=True)
            await asyncio.sleep(0.1)
            self.assertFalse(self.sent)
        finally:
            release.set()
        await incoming_task
        await self.until(lambda: len(self.calls("turn/start")) == 2)
        await self.rpc.finish()
        await self.until(lambda: self.sent)
        self.assertEqual(self.sent, ["结合图片后的完整答复"])

    async def test_completed_turn_rejecting_steer_continues_without_losing_input(self):
        self.rpc.steer_failure = "closed"
        await self.receive(1, "这是")
        self.worker()
        await self.until(lambda: self.calls("turn/start"))
        await self.receive(2, media=MessageType.IMAGE)
        await self.until(lambda: len(self.calls("turn/start")) == 2)
        self.assertIsNone(self.state.next_reply())
        await self.rpc.finish()
        await self.ready()
        self.assertEqual(self.state.next_reply()["text"], "结合图片后的完整答复")

    async def test_unknown_steer_outcome_is_not_replayed_and_stops_old_turn(self):
        self.rpc.steer_failure = "unknown"
        await self.receive(1, "这是")
        self.worker()
        await self.until(lambda: self.calls("turn/start"))
        await self.receive(2, media=MessageType.IMAGE)
        await self.ready()
        self.assertEqual(len(self.calls("turn/start")), 1)
        self.assertEqual(len(self.calls("turn/steer")), 1)
        self.assertEqual(len(self.calls("turn/interrupt")), 1)
        self.assertTrue(
            all(
                row[0] == "failed"
                for row in self.state.db.execute("SELECT status FROM inbox")
            )
        )

    async def test_stop_does_not_restart_queued_supplements_or_absorb_the_next_request(
        self,
    ):
        await self.receive(1, "这是")
        self.worker()
        await self.until(lambda: self.calls("turn/start"))
        await self.receive(2, media=MessageType.IMAGE)
        await self.receive(10, "/stop")
        await self.receive(3, "新的请求")
        await self.until(lambda: len(self.calls("turn/start")) == 2)
        self.assertFalse(self.calls("turn/steer"))
        self.assertEqual(self.calls("turn/start")[1]["clientUserMessageId"], "3")
        self.assertEqual(self.state.batch("1")["status"], "sealed")
        await self.rpc.finish("新请求的回复")
        await self.until(
            lambda: (
                self.state.db.execute(
                    "SELECT status FROM inbox WHERE message_id='3'"
                ).fetchone()[0]
                == "done"
            )
        )

    async def test_pre_steer_draft_is_not_concatenated_with_the_updated_answer(self):
        await self.receive(1, "这是")
        self.worker()
        await self.until(lambda: self.calls("turn/start"))
        await self.bridge.on_event(
            {
                "method": "item/completed",
                "params": {
                    "threadId": self.bridge.active["thread_id"],
                    "turnId": "turn-1",
                    "item": {
                        "type": "agentMessage",
                        "id": "draft",
                        "phase": "final_answer",
                        "text": "你说的是什么？",
                    },
                },
            }
        )
        await self.receive(2, media=MessageType.IMAGE)
        await self.until(lambda: self.calls("turn/steer"))
        await self.rpc.finish()
        await self.ready()
        self.assertEqual(self.state.next_reply()["text"], "结合图片后的完整答复")

    async def test_session_switch_does_not_merge_messages_across_threads(self):
        await self.receive(1, "第一会话")
        self.worker()
        await self.until(lambda: self.calls("turn/start"))
        await self.receive(10, "/new 第二会话")
        await self.receive(2, "第二会话消息")
        self.assertNotEqual(
            self.state.batch("1")["thread_id"], self.state.batch("2")["thread_id"]
        )
        self.assertFalse(self.calls("turn/steer"))
        await self.rpc.finish("第一会话答复")
        await self.until(lambda: len(self.calls("turn/start")) == 2)
        await self.rpc.finish("第二会话答复")
        await self.until(
            lambda: (
                self.state.db.execute(
                    "SELECT status FROM inbox WHERE message_id='2'"
                ).fetchone()[0]
                == "done"
            )
        )


if __name__ == "__main__":
    unittest.main()
