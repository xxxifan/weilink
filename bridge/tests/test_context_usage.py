from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from context_usage import context_summary, normalize_usage, saved_usage
from state import State
from wechat_bridge import Bridge


def report(last=33102, total=312214, window=475000):
    return {
        "last": {
            "inputTokens": last - 129,
            "outputTokens": 129,
            "cachedInputTokens": 30080,
            "reasoningOutputTokens": 68,
            "totalTokens": last,
        },
        "total": {"totalTokens": total},
        "modelContextWindow": window,
    }


class NoRpc:
    async def request(self, *args, **kwargs):
        raise AssertionError("Usage inspection must not call the App Server")


class ContextTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "bridge.sqlite"
        self.state = State(self.path)
        self.state.add_thread("owned-a", "微信 A")
        self.state.add_thread("owned-b", "微信 B")
        self.bridge = Bridge({"owner_user_id": "owner"}, self.state, rpc=NoRpc())
        self.loader = patch("wechat_bridge.saved_usage", return_value=None)
        self.loader.start()
        self.addCleanup(self.loader.stop)

    async def asyncTearDown(self):
        self.state.close()
        self.directory.cleanup()

    async def event(self, thread="owned-a", turn="t1", value=None):
        await self.bridge.on_event(
            {
                "method": "thread/tokenUsage/updated",
                "params": {
                    "threadId": thread,
                    "turnId": turn,
                    "tokenUsage": value or report(),
                },
            }
        )

    def test_context_ratio_uses_last_request_without_subtracting_cache_or_adding_reasoning(
        self,
    ):
        text = context_summary(normalize_usage(report(), 1000, "test"))
        self.assertIn("33,102 / 475,000", text)
        self.assertIn("7.0%", text)
        self.assertNotIn("312,214", text)

    def test_unknown_and_invalid_window_are_not_fabricated_as_zero(self):
        self.assertIn("暂无用量报告", context_summary(None))
        for window in (None, 0, -1, "475000", True):
            text = context_summary(normalize_usage(report(window=window), 1, "test"))
            self.assertIn("窗口上限未报告", text)
            self.assertNotIn("%", text)
        self.assertIsNone(
            normalize_usage({"last": {"totalTokens": -1}, "total": {}}, 1, "test")
        )

    async def test_usage_is_per_thread_and_persists_across_restart(self):
        await self.event()
        self.assertEqual(self.state.get("active_thread"), "owned-b")
        self.assertIn("暂无用量报告", self.bridge.status_text())
        self.state.put("active_thread", "owned-a")
        self.assertIn("7.0%", self.bridge.status_text())
        self.assertEqual(
            self.bridge.usage_record("owned-a")["total"]["totalTokens"], 312214
        )
        self.state.close()
        self.state = State(self.path)
        self.bridge.state = self.state
        self.assertIn("33,102 / 475,000", self.bridge.status_text())

    async def test_foreign_and_late_usage_cannot_change_current_turn_or_finish_reply(
        self,
    ):
        future = asyncio.get_running_loop().create_future()
        self.bridge.active = {"thread_id": "owned-a", "turn_id": "t2", "done": future}
        await self.event(thread="desktop-task")
        await self.event(turn="old-turn")
        self.assertIsNone(self.state.get("token_usage:desktop-task"))
        self.assertIsNone(self.state.get("token_usage:owned-a"))
        await self.event(thread="owned-b")
        self.assertIsNotNone(self.state.get("token_usage:owned-b"))
        self.assertEqual(self.bridge.active["turn_id"], "t2")
        self.assertFalse(future.done())

    async def test_compaction_can_lower_context_while_cumulative_usage_keeps_growing(
        self,
    ):
        self.state.put("active_thread", "owned-a")
        await self.event(value=report(last=100000, total=100000))
        await self.event(value=report(last=10000, total=110000))
        self.assertIn("10,000 / 475,000", self.bridge.status_text())
        self.assertEqual(
            self.bridge.usage_record("owned-a")["total"]["totalTokens"], 110000
        )

    async def test_status_shows_usage_without_rpc_and_removed_context_command_is_unknown(
        self,
    ):
        await self.event(thread="owned-b")
        self.bridge.active = {"thread_id": "owned-b", "turn_id": "t2"}
        await self.bridge.receive(
            SimpleNamespace(from_user="owner", text="/status", message_id="m1")
        )
        self.assertIsNone(self.state.next_message())
        self.assertEqual(self.state.get("active_thread"), "owned-b")
        self.assertIn("33,102 / 475,000", self.state.next_reply()["text"])
        await self.bridge.receive(
            SimpleNamespace(from_user="owner", text="/context", message_id="m2")
        )
        latest = self.state.db.execute(
            "SELECT text FROM outbox ORDER BY id DESC LIMIT 1"
        ).fetchone()[0]
        self.assertIn("不认识这个命令", latest)
        self.assertIsNone(self.state.next_message())


class SavedUsageTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.home = Path(self.directory.name)
        self.rollout = self.home / "owned.jsonl"
        with closing(sqlite3.connect(self.home / "state_5.sqlite")) as db, db:
            db.execute("CREATE TABLE threads (id TEXT PRIMARY KEY,rollout_path TEXT)")
            db.execute("INSERT INTO threads VALUES (?,?)", ("owned", str(self.rollout)))

    def tearDown(self):
        self.directory.cleanup()

    def event(self, last):
        return (
            json.dumps(
                {
                    "timestamp": "2026-10-22T10:00:00Z",
                    "type": "event_msg",
                    "payload": {
                        "type": "token_count",
                        "info": {
                            "last_token_usage": {
                                "total_tokens": last,
                                "input_tokens": last - 100,
                                "output_tokens": 100,
                            },
                            "total_token_usage": {"total_tokens": 200000},
                            "model_context_window": 475000,
                        },
                    },
                }
            )
            + "\n"
        )

    def test_fallback_reads_latest_valid_report_without_mutating_rollout_or_database(
        self,
    ):
        self.rollout.write_text(
            self.event(50000)
            + self.event(10000)
            + '{"type":"event_msg","payload":{"type":"token_count","info":null}}\n'
            + '{"partial',
            encoding="utf8",
        )
        paths = [self.rollout, self.home / "state_5.sqlite"]
        before = {path: path.read_bytes() for path in paths}
        usage = saved_usage("owned", self.home)
        self.assertEqual(usage["last"]["totalTokens"], 10000)
        self.assertEqual(usage["total"]["totalTokens"], 200000)
        self.assertEqual(usage["reported_at"], 1792663200)
        for path, content in before.items():
            self.assertEqual(path.read_bytes(), content)
        self.assertIsNone(saved_usage("foreign", self.home))

    def test_missing_or_incompatible_history_is_unknown_and_read_is_bounded(self):
        self.assertIsNone(saved_usage("owned", self.home))
        self.rollout.write_text(
            self.event(10000) + "x" * (1024 * 1024 + 1), encoding="utf8"
        )
        self.assertIsNone(saved_usage("owned", self.home))
        missing = self.home / "missing"
        missing.mkdir()
        self.assertIsNone(saved_usage("owned", missing))
        self.assertFalse((missing / "state_5.sqlite").exists())


if __name__ == "__main__":
    unittest.main()
