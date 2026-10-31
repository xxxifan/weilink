from __future__ import annotations

import sys
import tempfile
import unittest
from contextlib import asynccontextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from desktop_relay import DesktopRelay, DesktopUnavailable, reply_prompt
from notify import prepare_notification
from state import State


class Monitor:
    def find(self, reference, excluded):
        if reference in excluded or reference not in {"target", "other"}:
            raise ValueError("Not found")
        return {"id": reference, "title": "原任务", "cwd": "C:/project"}


class Client:
    def __init__(self):
        self.status = "idle"
        self.turns = []
        self.calls = []
        self.available = True
        self.send_error = False

    @asynccontextmanager
    async def session(self):
        if not self.available:
            raise DesktopUnavailable("Desktop closed")
        yield self

    async def call(self, session, name, args, caller):
        self.calls.append((name, args, caller))
        if name == "read_thread":
            return {
                "thread": {
                    "id": args["threadId"],
                    "hostId": "local",
                    "kind": "codex",
                    "status": {"type": self.status},
                },
                "turns": self.turns,
            }
        if name == "send_message_to_thread":
            if self.send_error:
                raise TimeoutError("Unknown outcome")
            return {"threadId": args["threadId"]}
        raise AssertionError(name)


class RelayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "bridge.sqlite"
        self.state = State(self.path)
        self.state.add_thread("wechat", "微信会话")
        self.client = Client()
        self.relay = DesktopRelay(self.state, Monitor(), self.client)
        self.task = {"id": "target", "title": "原任务"}

    async def asyncTearDown(self):
        self.state.close()
        self.directory.cleanup()

    def queue(self, message_id="123"):
        self.state.queue_desktop_reply(message_id, self.task, "wechat", "继续处理")
        return self.state.desktop_jobs()[0]

    def status(self):
        return self.state.db.execute(
            "SELECT status FROM desktop_replies ORDER BY created_at LIMIT 1"
        ).fetchone()[0]

    def sends(self):
        return [
            call for call in self.client.calls if call[0] == "send_message_to_thread"
        ]

    async def test_active_or_unknown_task_is_never_sent_resumed_or_interrupted(self):
        self.queue()
        for status in ("active", "unknown", "systemError"):
            self.client.status = status
            await self.relay.step()
        self.assertFalse(self.sends())
        self.assertEqual(self.status(), "queued")
        self.assertTrue(all(call[0] == "read_thread" for call in self.client.calls))
        self.client.status = "idle"
        self.client.turns = [{"status": "inProgress", "items": []}]
        await self.relay.step()
        self.assertFalse(self.sends())

    async def test_idle_send_uses_native_desktop_and_preserves_model_and_chat_routing(
        self,
    ):
        job = self.queue()
        await self.relay.step()
        await self.relay.step()
        self.assertEqual(len(self.sends()), 1)
        _, args, caller = self.sends()[0]
        self.assertEqual(set(args), {"threadId", "hostId", "prompt"})
        self.assertEqual(args["threadId"], "target")
        self.assertEqual(args["prompt"], reply_prompt(job))
        self.assertEqual(caller, "wechat")
        self.assertEqual(self.state.get("active_thread"), "wechat")
        self.assertEqual(self.status(), "sent")

    async def test_closed_desktop_keeps_queue_and_unknown_send_is_not_retried(self):
        self.queue()
        self.client.available = False
        with self.assertRaises(DesktopUnavailable):
            await self.relay.step()
        self.assertEqual(self.status(), "queued")
        self.client.available = True
        self.client.send_error = True
        with self.assertRaises(TimeoutError):
            await self.relay.step()
        self.assertEqual(self.status(), "uncertain")
        self.client.send_error = False
        await self.relay.step()
        self.assertEqual(len(self.sends()), 1)

    async def test_restart_marks_ambiguous_send_and_can_reconcile_public_answer(self):
        job = self.queue()
        self.state.desktop_state(job["id"], "sending")
        self.state.close()
        self.state = State(self.path)
        self.relay.state = self.state
        self.state.recover()
        self.assertEqual(self.status(), "uncertain")
        self.client.turns = [
            {
                "id": "reply-turn",
                "status": "completed",
                "items": [
                    {
                        "type": "userMessage",
                        "content": [{"type": "text", "text": reply_prompt(job)}],
                    },
                    {"type": "reasoning", "text": "HIDDEN_REASONING"},
                    {"type": "commandExecution", "output": "RAW_SECRET"},
                    {"type": "agentMessage", "phase": "commentary", "text": "中途进展"},
                    {
                        "type": "agentMessage",
                        "phase": "final_answer",
                        "text": "原任务结果",
                    },
                ],
            }
        ]
        await self.relay.step()
        self.assertEqual(self.status(), "done")
        self.assertFalse(self.sends())
        text = self.state.next_reply()["text"]
        self.assertIn("原任务结果", text)
        for secret in ("HIDDEN_REASONING", "RAW_SECRET", "中途进展"):
            self.assertNotIn(secret, text)
        await self.relay.step()
        self.assertEqual(
            self.state.db.execute("SELECT count(*) FROM outbox").fetchone()[0], 1
        )

    async def test_only_matching_user_message_is_returned_and_same_target_is_serial(
        self,
    ):
        self.queue()
        self.queue("124")
        self.client.turns = [
            {
                "status": "completed",
                "items": [
                    {
                        "type": "agentMessage",
                        "phase": "final_answer",
                        "text": "无关结果",
                    }
                ],
            }
        ]
        await self.relay.step()
        self.assertEqual(len(self.sends()), 1)
        await self.relay.step()
        self.assertEqual(len(self.sends()), 1)
        self.assertEqual(self.status(), "sent")

    async def test_native_cross_chat_wrapper_correlates_answer_without_leaking_tool_output(
        self,
    ):
        job = self.queue()
        self.state.desktop_state(job["id"], "sent")
        self.client.turns = [
            {
                "status": "completed",
                "items": [
                    {
                        "type": "functionCallOutput",
                        "name": "send_message_to_thread",
                        "namespace": "codex_app",
                        "output": {
                            "text": f"<codex_delegation>\n  <source_thread_id>wechat</source_thread_id>\n  <input>{reply_prompt(job)}</input>\n</codex_delegation>"
                        },
                    },
                    {"type": "agentMessage", "phase": "final_answer", "text": "完成"},
                ],
            }
        ]
        await self.relay.step()
        self.assertEqual(self.status(), "done")
        self.assertIn("完成", self.state.next_reply()["text"])
        self.assertNotIn("codex_delegation", self.state.next_reply()["text"])

    async def test_archived_target_does_not_block_other_tasks(self):
        self.task = {"id": "archived", "title": "已归档任务"}
        self.queue()
        self.task = {"id": "other", "title": "另一个任务"}
        self.queue("124")
        await self.relay.step()
        self.assertEqual(self.status(), "failed")
        self.assertEqual(len(self.sends()), 1)
        self.assertEqual(self.sends()[0][1]["threadId"], "other")

    async def test_cancel_only_queued_messages_does_not_stop_task(self):
        self.queue()
        self.assertIn("已取消", self.relay.cancel("r123"))
        self.assertEqual(self.status(), "cancelled")
        await self.relay.step()
        self.assertFalse(self.client.calls)
        self.queue("124")
        await self.relay.step()
        self.assertIn("未停止原任务", self.relay.cancel("r124"))

    async def test_legacy_alias_migration_and_two_connections_preserve_t13(self):
        self.state.put("task_aliases", {"t13": "target"})
        second = State(self.path)
        try:
            self.assertEqual(second.task_label("target"), "t13")
            self.assertEqual(self.state.task_label("target"), "t13")
            self.assertEqual(second.task_label("other"), "t14")
            self.assertEqual(self.state.resolve_task("t13"), "target")
        finally:
            second.close()

    async def test_notifier_binds_origin_without_sending_or_switching_wechat(self):
        config = {"owner_user_id": "owner", "bot_id": "bot"}
        result = prepare_notification(
            config, "任务完成", "target", self.state, Monitor()
        )
        self.assertFalse(result["sends_message"])
        self.assertEqual(result["task_alias"], "t1")
        self.assertIn("/reply t1", result["text"])
        self.assertEqual(self.state.get("active_thread"), "wechat")
        self.assertEqual(
            prepare_notification(config, "完成", None, self.state)["routing"], "unbound"
        )
        self.assertEqual(
            prepare_notification(config, "完成", "wechat", self.state)["routing"],
            "wechat-conversation",
        )


if __name__ == "__main__":
    unittest.main()
