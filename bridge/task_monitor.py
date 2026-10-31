"""Read desktop task snapshots without attaching to their executing App Server."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path


class MonitorUnavailable(RuntimeError):
    pass


def local_time(value: float | None) -> str:
    return datetime.fromtimestamp(value).strftime("%m-%d %H:%M:%S") if value else "未知"


def clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + "…（已截断）"


class TaskMonitor:
    def __init__(self, codex_home: Path | None = None, clock=time.time) -> None:
        self.home = codex_home or Path(
            os.environ.get("CODEX_HOME", Path.home() / ".codex")
        )
        self.clock = clock

    @contextmanager
    def database(self, name: str):
        # These are private, versioned stores, not a stable API. Fail closed if
        # their schema changes. Never repair, migrate, resume, or hydrate a
        # desktop thread: even an independent App Server reports its status
        # differently from the actual desktop owner.
        connection = None
        try:
            connection = sqlite3.connect(
                (self.home / name).resolve().as_uri() + "?mode=ro",
                uri=True,
                timeout=0.5,
            )
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA query_only=ON")
            connection.execute("BEGIN")
            yield connection
        except sqlite3.Error as exc:
            raise MonitorUnavailable(
                "本机任务记录暂不可读，或 Codex 存储格式已变化。"
            ) from exc
        finally:
            if connection is not None:
                connection.close()

    def tasks(self, excluded: set[str], query: str = "") -> list[dict]:
        running = query.lower() == "running"
        unfinished = None
        if running:
            with self.database("thread_history_1.sqlite") as history:
                unfinished = {
                    row[0]
                    for row in history.execute(
                        """SELECT t.thread_id FROM thread_turns t
                        WHERE t.status='inProgress' AND NOT EXISTS (
                            SELECT 1 FROM thread_turns newer
                            WHERE newer.thread_id=t.thread_id
                            AND newer.rollout_ordinal>t.rollout_ordinal)"""
                    )
                }
        with self.database("state_5.sqlite") as db:
            # source is JSON for subagents; those are intentionally omitted.
            rows = db.execute(
                """SELECT id,COALESCE(NULLIF(name,''),title) AS title,cwd
                FROM threads WHERE archived=0 AND source IN ('cli','vscode')
                AND (?='' OR title LIKE ? ESCAPE '\\' OR name LIKE ? ESCAPE '\\')
                ORDER BY COALESCE(recency_at_ms,updated_at_ms,updated_at*1000) DESC,id""",
                (
                    "" if running else query,
                    *(["%" + self.escape_like(query) + "%"] * 2),
                ),
            )
            selected = []
            for row in rows:
                if row["id"] in excluded:
                    continue
                if unfinished is not None and row["id"] not in unfinished:
                    continue
                selected.append(dict(row))
                if len(selected) == 12:
                    break
        return [self.snapshot(row) for row in selected]

    @staticmethod
    def escape_like(text: str) -> str:
        return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")

    def find(self, reference: str, excluded: set[str]) -> dict:
        if not re.fullmatch(r"[0-9a-f-]{8,36}", reference):
            raise ValueError("请用 /tasks 返回的 t编号，或至少 8 位任务 ID。")
        with self.database("state_5.sqlite") as db:
            rows = [
                dict(row)
                for row in db.execute(
                    """SELECT id,COALESCE(NULLIF(name,''),title) AS title,cwd
                    FROM threads WHERE id LIKE ? AND archived=0
                    AND source IN ('cli','vscode')""",
                    (reference + "%",),
                )
                if row["id"] not in excluded
            ]
        if len(rows) != 1:
            raise ValueError("任务未找到、已归档或 ID 不唯一。请重新用 /tasks 查询。")
        return rows[0]

    def snapshot(self, task: dict, history: bool = False) -> dict:
        result = {
            **task,
            "observed_at": self.clock(),
            "messages": [],
            "status": "unknown",
        }
        try:
            with self.database("thread_history_1.sqlite") as db:
                turn = db.execute(
                    """SELECT turn_id,status,started_at,completed_at FROM thread_turns
                    WHERE thread_id=? ORDER BY rollout_ordinal DESC LIMIT 1""",
                    (task["id"],),
                ).fetchone()
                if turn is None:
                    return result
                result.update(dict(turn))
                activity = db.execute(
                    """SELECT MAX(COALESCE(completed_at_ms,started_at_ms,created_at_ms))
                    FROM thread_items WHERE thread_id=? AND turn_id=?""",
                    (task["id"], turn["turn_id"]),
                ).fetchone()[0]
                result["activity_at"] = max(
                    (activity or 0) / 1000,
                    turn["completed_at"] or turn["started_at"] or 0,
                )
                # Only completed public chat items are displayed. Reasoning,
                # tool arguments/results, credentials and hidden instructions
                # must never become a monitoring transcript.
                rows = db.execute(
                    """SELECT item_json,created_at_ms FROM thread_items
                    WHERE thread_id=? AND (? OR turn_id=?)
                    AND item_type IN ('userMessage','agentMessage')
                    AND completed_at_ms IS NOT NULL
                    ORDER BY rollout_ordinal DESC LIMIT ?""",
                    (task["id"], history, turn["turn_id"], 8 if history else 4),
                ).fetchall()
                for row in reversed(rows):
                    item = json.loads(row["item_json"])
                    kind = item.get("type")
                    if kind == "agentMessage":
                        if item.get("phase") not in (
                            None,
                            "commentary",
                            "final_answer",
                        ):
                            continue
                        text = item.get("text", "")
                        role = "答复" if item.get("phase") == "final_answer" else "进展"
                    elif kind == "userMessage":
                        text = "\n".join(
                            part.get("text", "")
                            for part in item.get("content", [])
                            if part.get("type") == "text"
                        )
                        role = "用户"
                    else:
                        continue
                    if text.strip():
                        result["messages"].append(
                            {
                                "role": role,
                                "text": clip(text.strip(), 900),
                                "at": row["created_at_ms"] / 1000,
                            }
                        )
        except (MonitorUnavailable, ValueError, TypeError, KeyError):
            result.update(
                status="unknown", messages=[], warning="历史记录暂不可读或格式不兼容。"
            )
        return result

    def status(self, snapshot: dict) -> str:
        status = snapshot["status"]
        if status == "inProgress":
            recent = 0 <= self.clock() - snapshot.get("activity_at", 0) <= 180
            return "未结束，最近有活动记录" if recent else "未结束记录，活跃性待确认"
        return {
            "completed": "最新一轮已完成",
            "interrupted": "最新一轮已中断",
            "failed": "最新一轮失败",
        }.get(status, "状态未知")

    def detail(self, snapshot: dict, label: str, history: bool = False) -> str:
        lines = [
            f"任务 {label}：{snapshot['title']}",
            f"状态：{self.status(snapshot)}",
            f"记录活动时间：{local_time(snapshot.get('activity_at'))}",
            f"查询时间：{local_time(snapshot['observed_at'])}",
            f"目录：{snapshot['cwd']}",
            f"ID：{snapshot['id']}",
            "最近消息：" if history else "本轮已保存消息：",
        ]
        for message in snapshot["messages"]:
            lines.append(
                f"[{local_time(message['at'])} {message['role']}]\n{message['text']}"
            )
        if not snapshot["messages"]:
            lines.append(snapshot.get("warning", "暂无已保存的公开文字消息。"))
        lines.append("只读快照；等待确认、长时间工具执行、断线等无法仅凭此记录区分。")
        return "\n\n".join(lines)
