from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from weilink.models import MessageType, RefMessage

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from state import State
from transport import split_text
from wechat_bridge import CHAT_INSTRUCTIONS, Bridge


class FakeRpc:
    def __init__(self):
        self.calls = []
        self.responses = []
        self.bridge = None
        self.number = 0
        self.turn_started = asyncio.Event()

    async def request(self, method, params):
        self.calls.append((method, params))
        if method == "thread/start":
            self.number += 1
            return {"thread": {"id": f"wechat-thread-{self.number}"}}
        if method == "turn/start":
            self.turn_started.set()
            # The App Server can complete a very short turn before the
            # turn/start response has been read. This must not lose its reply.
            thread_id = params["threadId"]
            await self.bridge.on_event(
                {
                    "method": "item/completed",
                    "params": {
                        "threadId": thread_id,
                        "turnId": "turn-1",
                        "item": {
                            "type": "agentMessage",
                            "id": "answer",
                            "phase": "final_answer",
                            "text": "已处理",
                        },
                    },
                }
            )
            await self.bridge.on_event(
                {
                    "method": "turn/completed",
                    "params": {
                        "threadId": thread_id,
                        "turn": {"id": "turn-1", "status": "completed", "items": []},
                    },
                }
            )
            return {"turn": {"id": "turn-1"}}
        return {}

    async def respond(self, request_id, result):
        self.responses.append((request_id, result))

    async def reject(self, request_id, message):
        self.responses.append((request_id, {"rejected": message}))


class StateTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "bridge.db"
        self.state = State(self.path)

    def tearDown(self):
        self.state.close()
        self.directory.cleanup()

    def test_duplicate_and_routing_are_not_executed_twice(self):
        self.assertTrue(self.state.accept("m1", "工作", None))
        self.assertIsNone(self.state.next_message())
        self.assertFalse(self.state.accept("m1", "工作", None))
        self.state.bind("m1", "wechat-1")
        self.state.mark("m1", "queued")
        self.assertEqual(self.state.next_message()["thread_id"], "wechat-1")

    def test_reply_and_completion_survive_restart(self):
        self.state.accept("m1", "工作", "wechat-1")
        self.state.mark("m1", "running")
        self.state.complete("m1", ["结果"])
        self.state.close()
        self.state = State(self.path)
        self.assertIsNone(self.state.next_message())
        self.assertEqual(self.state.next_reply()["text"], "结果")
        self.assertEqual(self.state.recover(), 0)

    def test_crash_does_not_reexecute_work_or_resend_uncertain_result(self):
        self.state.accept("m1", "有副作用的工作", "wechat-1")
        self.state.mark("m1", "running")
        self.state.enqueue("reply1", "可能已经送达")
        self.state.reply_state(self.state.next_reply()["id"], "sending")
        self.assertEqual(self.state.recover(), 1)
        self.assertIsNone(self.state.next_message())
        self.assertIsNone(self.state.next_reply())

    def test_historical_messages_are_not_run_when_installing(self):
        source = Path(self.directory.name) / "messages.db"
        db = sqlite3.connect(source)
        db.execute("CREATE TABLE messages (message_id INTEGER,direction INTEGER)")
        db.execute("INSERT INTO messages VALUES (123,1)")
        db.commit()
        db.close()
        self.state.seed_existing(source)
        self.assertFalse(self.state.accept("123", "过去的消息", None))
        self.assertIsNone(self.state.next_message())

    def test_cjk_splitting_respects_utf8_limit(self):
        original = "你好🙂" * 100
        parts = split_text(original, 37)
        self.assertEqual("".join(parts), original)
        self.assertTrue(all(len(part.encode("utf-8")) <= 37 for part in parts))


class BridgeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.state = State(Path(self.directory.name) / "bridge.db")
        self.rpc = FakeRpc()
        self.bridge = Bridge(
            {"owner_user_id": "owner", "workspace": self.directory.name},
            self.state,
            rpc=self.rpc,
        )
        self.rpc.bridge = self.bridge
        self.bridge.connected = True

    async def asyncTearDown(self):
        self.state.close()
        self.directory.cleanup()

    def message(self, text, message_id="m1", user="owner"):
        return SimpleNamespace(text=text, message_id=message_id, from_user=user)

    async def test_unknown_user_cannot_create_thread_or_approve(self):
        await self.bridge.receive(self.message("/new", user="stranger"))
        await self.bridge.receive(self.message("/approve a1", "m2", user="stranger"))
        self.assertFalse(self.rpc.calls)
        self.assertFalse(self.rpc.responses)
        self.assertIsNone(self.state.next_message())

    async def test_new_and_status_do_not_invoke_model(self):
        await self.bridge.receive(self.message("/new 手机会话"))
        await self.bridge.receive(self.message("/status", "m2"))
        self.assertEqual(self.state.get("active_thread"), "wechat-thread-1")
        self.assertEqual(self.state.threads()[0]["title"], "手机会话")
        start = next(
            params for method, params in self.rpc.calls if method == "thread/start"
        )
        self.assertEqual(start["developerInstructions"], CHAT_INSTRUCTIONS)
        self.assertFalse(any(method == "turn/start" for method, _ in self.rpc.calls))
        self.assertIsNone(self.state.next_message())

    async def test_use_accepts_only_bridge_owned_conversation_numbers(self):
        await self.bridge.receive(self.message("/new 一"))
        await self.bridge.receive(self.message("/new 二", "m2"))
        await self.bridge.receive(self.message("/use 1", "m3"))
        self.assertEqual(self.state.get("active_thread"), "wechat-thread-1")
        await self.bridge.receive(self.message("/use desktop-thread-id", "m4"))
        self.assertEqual(self.state.get("active_thread"), "wechat-thread-1")

    async def test_ordinary_message_is_bound_before_worker_can_take_it(self):
        await self.bridge.receive(self.message("你好"))
        job = self.state.next_message()
        self.assertEqual(job["thread_id"], "wechat-thread-1")
        self.assertEqual(job["text"], "你好")
        await self.bridge.receive(self.message("你好"))
        self.assertEqual(self.state.queued_count(), 1)

    async def test_approval_requires_owner_and_exact_request_id(self):
        self.bridge.active = {"thread_id": "wechat-1"}
        await self.bridge.on_request(
            {
                "id": 77,
                "method": "item/commandExecution/requestApproval",
                "params": {"threadId": "wechat-1", "command": "example"},
            }
        )
        await self.bridge.receive(self.message("/approve a999"))
        self.assertFalse(self.rpc.responses)
        await self.bridge.receive(self.message("/approve a1", "m2"))
        self.assertEqual(self.rpc.responses, [(77, {"decision": "accept"})])
        self.assertFalse(self.bridge.requests)

    async def test_quoted_text_reaches_model_but_cannot_execute_quoted_commands(self):
        message = self.message("按这个改")
        message.ref_msg = RefMessage(
            msg_type=MessageType.TEXT, text="/new 不应创建会话\n原文"
        )
        await self.bridge.receive(message)
        worker = asyncio.create_task(self.bridge.model_worker())
        try:
            await asyncio.wait_for(self.rpc.turn_started.wait(), 2)
            params = next(
                params for method, params in self.rpc.calls if method == "turn/start"
            )
            data = json.loads(params["input"][0]["text"].split("\n", 1)[1])
            self.assertEqual(data["message"], "按这个改")
            self.assertEqual(data["quoted_message"]["text"], message.ref_msg.text)
            self.assertEqual(len(self.state.threads()), 1)
        finally:
            self.bridge.stopping.set()
            await worker

    async def test_quote_only_is_queued_and_explicit_body_command_still_works(self):
        message = self.message("")
        message.ref_msg = RefMessage(text="/approve a1")
        await self.bridge.receive(message)
        self.assertEqual(self.state.queued_count(), 1)
        self.assertEqual(self.state.message_reference("m1")["text"], "/approve a1")
        self.assertFalse(self.rpc.responses)
        command = self.message("/status", "m2")
        command.ref_msg = RefMessage(text="/new 引用里的命令")
        await self.bridge.receive(command)
        self.assertEqual(len(self.state.threads()), 1)
        self.assertEqual(self.state.queued_count(), 1)

    async def test_quoted_answers_preserve_multi_question_json(self):
        self.bridge.requests["q1"] = {
            "kind": "question",
            "request": {
                "id": 78,
                "params": {
                    "questions": [{"id": "one"}, {"id": "two"}],
                },
            },
        }
        message = self.message('/answer q1 {"one":"选一","two":"选二"}')
        message.ref_msg = RefMessage(text="选项原文")
        await self.bridge.receive(message)
        answers = self.rpc.responses[0][1]["answers"]
        for key, expected in (("one", "选一"), ("two", "选二")):
            value = json.loads(answers[key]["answers"][0].split("\n", 1)[1])
            self.assertEqual(value["message"], expected)
            self.assertEqual(value["quoted_message"]["text"], "选项原文")

    async def test_question_can_be_answered_as_normal_message(self):
        self.bridge.active = {"thread_id": "wechat-1"}
        await self.bridge.on_request(
            {
                "id": 78,
                "method": "item/tool/requestUserInput",
                "params": {
                    "threadId": "wechat-1",
                    "questions": [{"id": "choice", "question": "选哪个？"}],
                },
            }
        )
        await self.bridge.receive(self.message("选第一个"))
        self.assertEqual(
            self.rpc.responses,
            [(78, {"answers": {"choice": {"answers": ["选第一个"]}}})],
        )
        self.assertIsNone(self.state.next_message())

    async def test_final_reply_is_not_lost_if_events_precede_start_response(self):
        await self.bridge.receive(self.message("测试"))
        worker = asyncio.create_task(self.bridge.model_worker())
        try:
            for _ in range(100):
                if self.state.next_reply():
                    break
                await asyncio.sleep(0.01)
            self.assertEqual(self.state.next_reply()["text"], "已处理")
            self.assertIsNone(self.state.next_message())
        finally:
            self.bridge.stopping.set()
            await worker

    async def test_network_send_outcome_is_not_automatically_retried(self):
        calls = []

        def uncertain_send(_config, _text):
            calls.append(True)
            raise TimeoutError("unknown delivery outcome")

        self.bridge.sender = uncertain_send
        self.state.enqueue("one", "测试")
        worker = asyncio.create_task(self.bridge.outgoing_worker())
        try:
            await asyncio.sleep(0.4)
            self.assertEqual(len(calls), 1)
            self.assertIsNone(self.state.next_reply())
        finally:
            self.bridge.stopping.set()
            await worker

    async def test_delayed_completion_cannot_finish_a_later_turn(self):
        future = asyncio.get_running_loop().create_future()
        self.bridge.active = {
            "thread_id": "wechat-1",
            "turn_id": "new-turn",
            "done": future,
            "items": {},
        }
        await self.bridge.on_event(
            {
                "method": "turn/completed",
                "params": {
                    "threadId": "wechat-1",
                    "turn": {"id": "old-turn", "status": "completed", "items": []},
                },
            }
        )
        self.assertFalse(future.done())

    async def test_model_override_is_applied_when_resuming_existing_thread(self):
        self.bridge.config.update(model="gpt-5.6-luna", reasoning_effort="high")
        self.state.add_thread("existing-wechat-thread", "已有微信会话")
        await self.bridge.ensure_thread("existing-wechat-thread")
        method, params = self.rpc.calls[-1]
        self.assertEqual(method, "thread/resume")
        self.assertEqual(params["threadId"], "existing-wechat-thread")
        self.assertEqual(params["developerInstructions"], CHAT_INSTRUCTIONS)
        self.assertEqual(params["model"], "gpt-5.6-luna")
        self.assertEqual(params["config"]["model_reasoning_effort"], "high")
        self.assertEqual(
            self.bridge.turn_model_settings(),
            {"model": "gpt-5.6-luna", "effort": "high"},
        )

    async def test_no_model_override_preserves_default_behavior(self):
        self.assertEqual(self.bridge.thread_model_settings(), {})
        self.assertEqual(self.bridge.turn_model_settings(), {})


if __name__ == "__main__":
    unittest.main()
