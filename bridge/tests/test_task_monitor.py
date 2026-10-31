from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from state import State
from task_monitor import MonitorUnavailable, TaskMonitor
from wechat_bridge import Bridge

ACTIVE = "aaaaaaaa-0000-0000-0000-000000000001"
IDLE = "bbbbbbbb-0000-0000-0000-000000000002"
OWNED = "cccccccc-0000-0000-0000-000000000003"


class RecordingRpc:
    def __init__(self):
        self.calls = []

    async def request(self, method, params):
        self.calls.append((method, params))
        if method == "thread/resume":
            return {"thread": {"id": params["threadId"]}}
        return {}


class MonitorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.home = Path(self.directory.name)
        self.metadata = self.home / "state_5.sqlite"
        self.history = self.home / "thread_history_1.sqlite"
        with closing(sqlite3.connect(self.metadata)) as db, db:
            db.executescript("""
                CREATE TABLE threads (id TEXT PRIMARY KEY,title TEXT,name TEXT,cwd TEXT,
                    archived INT,source TEXT,recency_at_ms INT,updated_at_ms INT,updated_at INT);
            """)
            db.executemany(
                "INSERT INTO threads VALUES (?,?,?,?,?,?,?,?,?)",
                [
                    (
                        ACTIVE,
                        "保存标题",
                        "原样任务名",
                        "C:/project",
                        0,
                        "vscode",
                        900000,
                        999999,
                        999,
                    ),
                    (IDLE, "另一任务", None, "C:/other", 0, "cli", 800000, 800000, 800),
                    (
                        OWNED,
                        "微信会话",
                        None,
                        "C:/wechat",
                        0,
                        "cli",
                        950000,
                        950000,
                        950,
                    ),
                    (
                        "dddddddd-0000",
                        "子代理",
                        None,
                        "C:/project",
                        0,
                        '{"subagent":{}}',
                        999999,
                        999999,
                        999,
                    ),
                    (
                        "eeeeeeee-0000",
                        "已归档",
                        None,
                        "C:/project",
                        1,
                        "vscode",
                        999999,
                        999999,
                        999,
                    ),
                ],
            )
        with closing(sqlite3.connect(self.history)) as db, db:
            db.executescript("""
                CREATE TABLE thread_turns (thread_id TEXT,turn_id TEXT,rollout_ordinal INT,
                    status TEXT,started_at INT,completed_at INT);
                CREATE TABLE thread_items (thread_id TEXT,turn_id TEXT,rollout_ordinal INT,
                    item_type TEXT,item_json TEXT,created_at_ms INT,started_at_ms INT,completed_at_ms INT);
            """)
            db.executemany(
                "INSERT INTO thread_turns VALUES (?,?,?,?,?,?)",
                [
                    (ACTIVE, "previous", 1, "completed", 500, 510),
                    (ACTIVE, "latest", 10, "inProgress", 950, None),
                    (IDLE, "previous", 1, "inProgress", 700, None),
                    (IDLE, "latest", 10, "completed", 800, 810),
                ],
            )
            self.add_item(
                db,
                ACTIVE,
                "previous",
                2,
                {"type": "agentMessage", "phase": "final_answer", "text": "上轮结果"},
                510000,
            )
            self.add_item(
                db,
                ACTIVE,
                "latest",
                11,
                {
                    "type": "userMessage",
                    "content": [{"type": "text", "text": "用户请求"}],
                },
                950000,
            )
            self.add_item(
                db,
                ACTIVE,
                "latest",
                12,
                {"type": "agentMessage", "phase": "commentary", "text": "正在检查"},
                990000,
            )
            self.add_item(
                db,
                ACTIVE,
                "latest",
                13,
                {"type": "reasoning", "text": "HIDDEN_REASONING"},
                990000,
            )
            self.add_item(
                db,
                ACTIVE,
                "latest",
                14,
                {"type": "commandExecution", "text": "RAW_TOOL_SECRET"},
                990000,
            )
            self.add_item(
                db,
                ACTIVE,
                "latest",
                15,
                {"type": "agentMessage", "text": "PARTIAL_TEXT"},
                990000,
                complete=False,
            )
        self.monitor = TaskMonitor(self.home, clock=lambda: 1000)
        self.state = State(self.home / "bridge.sqlite")
        self.state.add_thread(OWNED, "微信会话")
        self.rpc = RecordingRpc()
        self.bridge = Bridge(
            {"owner_user_id": "owner", "workspace": str(self.home)},
            self.state,
            rpc=self.rpc,
            task_monitor=self.monitor,
        )
        self.sequence = 0

    @staticmethod
    def add_item(db, thread, turn, ordinal, item, at, complete=True):
        db.execute(
            "INSERT INTO thread_items VALUES (?,?,?,?,?,?,?,?)",
            (
                thread,
                turn,
                ordinal,
                item["type"],
                json.dumps(item),
                at,
                at,
                at if complete else None,
            ),
        )

    async def asyncTearDown(self):
        self.state.close()
        self.directory.cleanup()

    async def send(self, text):
        self.sequence += 1
        await self.bridge.receive(
            SimpleNamespace(
                text=text, message_id=f"m{self.sequence}", from_user="owner"
            )
        )

    def replies(self):
        return "\n".join(
            row[0]
            for row in self.state.db.execute("SELECT text FROM outbox ORDER BY id")
        )

    def test_direct_connections_reject_writes_and_never_create_missing_databases(self):
        with self.monitor.database("state_5.sqlite") as db:
            with self.assertRaises(sqlite3.OperationalError):
                db.execute("UPDATE threads SET title='changed'")
        with self.assertRaises(MonitorUnavailable):
            with self.monitor.database("missing.sqlite"):
                self.fail("A missing store must not be created")
        self.assertFalse((self.home / "missing.sqlite").exists())

    def test_only_latest_turn_decides_unfinished_filter(self):
        tasks = self.monitor.tasks({OWNED}, "running")
        self.assertEqual([task["id"] for task in tasks], [ACTIVE])
        self.assertEqual(tasks[0]["title"], "原样任务名")
        self.assertIn("最近有活动记录", self.monitor.status(tasks[0]))
        self.monitor.clock = lambda: 2000
        self.assertIn("活跃性待确认", self.monitor.status(tasks[0]))

    def test_public_transcript_excludes_reasoning_tools_and_partial_items(self):
        task = self.monitor.find(ACTIVE, {OWNED})
        current = self.monitor.snapshot(task)
        recent = self.monitor.snapshot(task, history=True)
        text = self.monitor.detail(current, "t1")
        self.assertIn("正在检查", text)
        for hidden in (
            "HIDDEN_REASONING",
            "RAW_TOOL_SECRET",
            "PARTIAL_TEXT",
            "上轮结果",
        ):
            self.assertNotIn(hidden, text)
        self.assertIn("上轮结果", self.monitor.detail(recent, "t1", True))

    def test_query_is_parameterized_and_wildcards_are_literal(self):
        self.assertEqual(self.monitor.tasks({OWNED}, "%' OR 1=1 --"), [])
        self.assertEqual(self.monitor.tasks({OWNED}, "%"), [])
        self.assertEqual(len(self.monitor.tasks({OWNED}, "原样任务名")), 1)
        with self.assertRaises(ValueError):
            self.monitor.find("aaaa", set())
        with self.assertRaises(ValueError):
            self.monitor.find(OWNED, {OWNED})

    async def test_monitoring_original_does_not_issue_any_app_server_rpc(self):
        before = {path: path.read_bytes() for path in (self.metadata, self.history)}
        self.bridge.active = {"thread_id": OWNED, "turn_id": "wechat-turn"}
        for text in (
            "/tasks",
            "/tasks running",
            "/task t1",
            "/progress",
            "/history",
            "/status",
        ):
            await self.send(text)
        self.assertFalse(self.rpc.calls)
        self.assertEqual(self.state.get("active_thread"), OWNED)
        self.assertEqual(self.state.get("monitored_task"), ACTIVE)
        self.assertIn("普通文字仍进入独立微信会话", self.replies())
        for path, content in before.items():
            self.assertEqual(path.read_bytes(), content)
        await self.send("/stop")
        self.assertEqual(
            self.rpc.calls,
            [("turn/interrupt", {"threadId": OWNED, "turnId": "wechat-turn"})],
        )

    async def test_aliases_survive_reorder_and_restart_without_redirecting_chat(self):
        await self.send("/tasks")
        with closing(sqlite3.connect(self.metadata)) as db, db:
            db.execute("UPDATE threads SET recency_at_ms=1000000 WHERE id=?", (IDLE,))
        await self.send("/tasks")
        self.state.close()
        self.state = State(self.home / "bridge.sqlite")
        self.bridge.state = self.state
        await self.send("/task t1")
        self.assertEqual(self.state.get("monitored_task"), ACTIVE)
        await self.send("继续聊")
        self.assertEqual(self.state.next_message()["thread_id"], OWNED)
        self.assertTrue(
            all(params.get("threadId") != ACTIVE for _, params in self.rpc.calls)
        )
        await self.send("/use t1")
        self.assertEqual(self.state.get("active_thread"), OWNED)
        await self.send("/unwatch")
        self.assertIsNone(self.state.get("monitored_task"))

    async def test_no_selection_and_incompatible_schema_fail_without_rpc(self):
        await self.send("/progress")
        await self.send("/task unknown-id")
        with closing(sqlite3.connect(self.metadata)) as db, db:
            db.execute("DROP TABLE threads")
        await self.send("/tasks")
        self.assertIn("先用 /tasks", self.replies())
        self.assertIn("存储格式已变化", self.replies())
        self.assertFalse(self.rpc.calls)

    async def test_tasks_t13_is_a_stable_task_reference_not_a_title_filter(self):
        for index in range(12):
            self.state.task_label(f"past-task-{index}")
        self.assertEqual(self.state.task_label(ACTIVE), "t13")
        await self.send("/tasks t13")
        self.assertEqual(self.state.get("monitored_task"), ACTIVE)
        self.assertIn("任务 t13：原样任务名", self.replies())
        self.assertNotIn("没有找到匹配任务", self.replies())
        await self.send("/task t13")
        self.assertFalse(self.rpc.calls)

    async def test_reply_requires_owner_explicit_target_and_keeps_normal_routing(self):
        self.state.task_label(ACTIVE)
        await self.send("/reply t1 请继续检查")
        self.assertEqual(len(self.state.desktop_jobs()), 1)
        job = self.state.desktop_jobs()[0]
        self.assertEqual(job["thread_id"], ACTIVE)
        self.assertEqual(job["caller_thread_id"], OWNED)
        self.assertEqual(job["prompt"], "请继续检查")
        await self.send("/reply t999 不能发")
        self.assertEqual(len(self.state.desktop_jobs()), 1)
        await self.send("/replies")
        await self.send(f"/cancel r{job['id']}")
        self.assertFalse(self.state.desktop_jobs())
        await self.send("普通聊天")
        self.assertEqual(self.state.next_message()["thread_id"], OWNED)
        self.assertTrue(all(p.get("threadId") != ACTIVE for _, p in self.rpc.calls))

    async def test_explicit_desktop_reply_retains_reference_without_changing_target(
        self,
    ):
        self.state.task_label(ACTIVE)
        await self.bridge.receive(
            SimpleNamespace(
                text="/reply t1 按这个改",
                message_id="quote-reply",
                from_user="owner",
                quoted_context={"type": "TEXT", "text": "/reply t999 引用不能改变目标"},
            )
        )
        job = self.state.desktop_jobs()[0]
        self.assertEqual(job["thread_id"], ACTIVE)
        prompt = json.loads(job["prompt"].split("\n", 1)[1])
        self.assertEqual(prompt["message"], "按这个改")
        self.assertEqual(
            prompt["quoted_message"]["text"], "/reply t999 引用不能改变目标"
        )


if __name__ == "__main__":
    unittest.main()
