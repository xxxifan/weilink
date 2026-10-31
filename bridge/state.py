"""Durable deduplication, conversation selection, and reply outbox."""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path


class State:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE IF NOT EXISTS threads (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                thread_id TEXT UNIQUE NOT NULL, title TEXT NOT NULL,
                created_at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS inbox (
                message_id TEXT PRIMARY KEY, thread_id TEXT,
                text TEXT NOT NULL, status TEXT NOT NULL,
                received_at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS outbox (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                dedupe_key TEXT UNIQUE NOT NULL,
                text TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
                error TEXT, created_at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS task_aliases (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                thread_id TEXT UNIQUE NOT NULL);
            CREATE TABLE IF NOT EXISTS notification_origins (
                thread_id TEXT PRIMARY KEY, title TEXT NOT NULL,
                updated_at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS desktop_replies (
                id TEXT PRIMARY KEY, thread_id TEXT NOT NULL,
                caller_thread_id TEXT NOT NULL, title TEXT NOT NULL,
                prompt TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'queued',
                created_at REAL NOT NULL, sent_at REAL, error TEXT);
            CREATE TABLE IF NOT EXISTS message_batches (
                id TEXT PRIMARY KEY, thread_id TEXT NOT NULL,
                status TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 0,
                created_at REAL NOT NULL);
            """
        )
        # Existing notification writers can keep inserting text-only rows.
        # Media uses the same durable outbox and uncertain-send recovery rules.
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(outbox)")}
        with self.db:
            if "kind" not in columns:
                self.db.execute(
                    "ALTER TABLE outbox ADD COLUMN kind TEXT NOT NULL DEFAULT 'text'"
                )
            if "media_path" not in columns:
                self.db.execute("ALTER TABLE outbox ADD COLUMN media_path TEXT")
            if "batch_id" not in columns:
                self.db.execute("ALTER TABLE outbox ADD COLUMN batch_id TEXT")
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(inbox)")}
        with self.db:
            if "quoted_context" not in columns:
                self.db.execute("ALTER TABLE inbox ADD COLUMN quoted_context TEXT")
            if "has_image" not in columns:
                self.db.execute(
                    "ALTER TABLE inbox ADD COLUMN has_image INTEGER NOT NULL DEFAULT 0"
                )
            if "image_path" not in columns:
                self.db.execute("ALTER TABLE inbox ADD COLUMN image_path TEXT")
            if "has_voice" not in columns:
                self.db.execute(
                    "ALTER TABLE inbox ADD COLUMN has_voice INTEGER NOT NULL DEFAULT 0"
                )
            if "voice_path" not in columns:
                self.db.execute("ALTER TABLE inbox ADD COLUMN voice_path TEXT")
            if "transcription" not in columns:
                self.db.execute("ALTER TABLE inbox ADD COLUMN transcription TEXT")
            if "has_video" not in columns:
                self.db.execute(
                    "ALTER TABLE inbox ADD COLUMN has_video INTEGER NOT NULL DEFAULT 0"
                )
            if "video_path" not in columns:
                self.db.execute("ALTER TABLE inbox ADD COLUMN video_path TEXT")
            if "batch_id" not in columns:
                self.db.execute("ALTER TABLE inbox ADD COLUMN batch_id TEXT")
            if "submitted" not in columns:
                self.db.execute(
                    "ALTER TABLE inbox ADD COLUMN submitted INTEGER NOT NULL DEFAULT 0"
                )
            self.db.execute(
                "CREATE INDEX IF NOT EXISTS inbox_batch ON inbox(batch_id,submitted,received_at)"
            )
            self.db.execute(
                "CREATE INDEX IF NOT EXISTS inbox_routing ON inbox(thread_id,status)"
            )
            self.db.execute(
                "CREATE INDEX IF NOT EXISTS batches_conversation ON message_batches(thread_id,status)"
            )
            self.db.execute(
                "CREATE INDEX IF NOT EXISTS outbox_batch ON outbox(batch_id,status)"
            )
        # Preserve all previously issued t-numbers. A table with a UNIQUE
        # thread ID also prevents the notifier and receiver allocating the
        # same number to different tasks in concurrent processes.
        with self.db:
            for label, thread_id in self.get("task_aliases", {}).items():
                if label.startswith("t") and label[1:].isdigit():
                    self.db.execute(
                        "INSERT OR IGNORE INTO task_aliases VALUES (?,?)",
                        (int(label[1:]), thread_id),
                    )

    def task_label(self, thread_id: str) -> str:
        with self.db:
            row = self.db.execute(
                "SELECT id FROM task_aliases WHERE thread_id=?", (thread_id,)
            ).fetchone()
            if row is None:
                self.db.execute(
                    "INSERT OR IGNORE INTO task_aliases (thread_id) VALUES (?)",
                    (thread_id,),
                )
                row = self.db.execute(
                    "SELECT id FROM task_aliases WHERE thread_id=?", (thread_id,)
                ).fetchone()
        return f"t{row[0]}"

    def resolve_task(self, reference: str) -> str:
        if reference.startswith("t") and reference[1:].isdigit():
            row = self.db.execute(
                "SELECT thread_id FROM task_aliases WHERE id=?", (int(reference[1:]),)
            ).fetchone()
            if row is None:
                raise ValueError(
                    "未找到这个任务编号。编号无 12 的上限，请先用 /tasks 查询。"
                )
            return row[0]
        return reference

    def register_origin(self, thread_id: str, title: str) -> str:
        label = self.task_label(thread_id)
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO notification_origins VALUES (?,?,?)",
                (thread_id, title, time.time()),
            )
        return label

    def queue_desktop_reply(
        self, message_id: str, task: dict, caller: str, text: str
    ) -> None:
        with self.db:
            self.db.execute(
                """INSERT OR IGNORE INTO desktop_replies
                (id,thread_id,caller_thread_id,title,prompt,created_at) VALUES (?,?,?,?,?,?)""",
                (message_id, task["id"], caller, task["title"], text, time.time()),
            )

    def desktop_jobs(self) -> list[dict]:
        return [
            dict(row)
            for row in self.db.execute(
                "SELECT * FROM desktop_replies WHERE status IN ('queued','sending','sent','uncertain') ORDER BY created_at,id"
            )
        ]

    def desktop_state(
        self, message_id: str, status: str, error: str | None = None
    ) -> None:
        with self.db:
            self.db.execute(
                """UPDATE desktop_replies SET status=?,error=?,
                sent_at=CASE WHEN ?='sent' THEN COALESCE(sent_at,?) ELSE sent_at END WHERE id=?""",
                (status, error, status, time.time(), message_id),
            )

    def desktop_complete(
        self, message_id: str, replies: list[str], status: str = "done"
    ) -> None:
        with self.db:
            for index, text in enumerate(replies):
                self.db.execute(
                    "INSERT OR IGNORE INTO outbox (dedupe_key,text,created_at) VALUES (?,?,?)",
                    (f"desktop-result:{message_id}:{index}", text, time.time()),
                )
            self.db.execute(
                "UPDATE desktop_replies SET status=? WHERE id=?", (status, message_id)
            )

    def get(self, key: str, default=None):
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def put(self, key: str, value) -> None:
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO meta VALUES (?,?)", (key, json.dumps(value))
            )

    def add_thread(self, thread_id: str, title: str) -> None:
        with self.db:
            self.db.execute(
                "INSERT OR IGNORE INTO threads (thread_id,title,created_at) VALUES (?,?,?)",
                (thread_id, title, time.time()),
            )
        self.put("active_thread", thread_id)

    def threads(self) -> list[dict]:
        return [
            dict(row)
            for row in self.db.execute("SELECT * FROM threads ORDER BY id DESC")
        ]

    def accept(
        self,
        message_id: str,
        text: str,
        thread_id: str | None,
        quoted_context: dict | None = None,
        has_image: bool = False,
        has_voice: bool = False,
        has_video: bool = False,
    ) -> bool:
        with self.db:
            result = self.db.execute(
                "INSERT OR IGNORE INTO inbox (message_id,thread_id,text,status,received_at,quoted_context,has_image,has_voice,has_video) VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    message_id,
                    thread_id,
                    text,
                    "routing",
                    time.time(),
                    json.dumps(quoted_context, ensure_ascii=False)
                    if quoted_context is not None
                    else None,
                    int(has_image),
                    int(has_voice),
                    int(has_video),
                ),
            )
        return bool(result.rowcount)

    def message_reference(self, message_id: str) -> dict | None:
        row = self.db.execute(
            "SELECT quoted_context FROM inbox WHERE message_id=?", (message_id,)
        ).fetchone()
        return json.loads(row[0]) if row and row[0] is not None else None

    def save_image_path(self, message_id: str, path: str) -> None:
        with self.db:
            self.db.execute(
                "UPDATE inbox SET image_path=? WHERE message_id=? AND has_image=1",
                (path, message_id),
            )

    def save_voice(self, message_id: str, path: str, text: str) -> None:
        with self.db:
            self.db.execute(
                "UPDATE inbox SET voice_path=?,transcription=? WHERE message_id=? AND has_voice=1",
                (path, text, message_id),
            )

    def save_video_path(self, message_id: str, path: str) -> None:
        with self.db:
            self.db.execute(
                "UPDATE inbox SET video_path=? WHERE message_id=? AND has_video=1",
                (path, message_id),
            )

    def save_reference(self, message_id: str, reference: dict) -> None:
        with self.db:
            self.db.execute(
                "UPDATE inbox SET quoted_context=? WHERE message_id=?",
                (json.dumps(reference, ensure_ascii=False), message_id),
            )

    def bind(self, message_id: str, thread_id: str) -> None:
        with self.db:
            self.db.execute(
                "UPDATE inbox SET thread_id=? WHERE message_id=?",
                (thread_id, message_id),
            )

    def mark(self, message_id: str, status: str) -> None:
        with self.db:
            self.db.execute(
                "UPDATE inbox SET status=? WHERE message_id=?", (status, message_id)
            )

    def next_message(self) -> dict | None:
        row = self.db.execute(
            "SELECT * FROM inbox WHERE status='queued' ORDER BY received_at LIMIT 1"
        ).fetchone()
        return dict(row) if row else None

    def queued_count(self) -> int:
        return self.db.execute(
            "SELECT count(*) FROM inbox WHERE status='queued'"
        ).fetchone()[0]

    def queue_message(self, message_id: str) -> str:
        """Join the same conversation until its first reply starts sending."""
        with self.db:
            message = self.db.execute(
                "SELECT * FROM inbox WHERE message_id=?", (message_id,)
            ).fetchone()
            if message["batch_id"]:
                return message["batch_id"]
            batch = self.db.execute(
                "SELECT * FROM message_batches WHERE thread_id=? AND status IN ('queued','processing','ready') ORDER BY created_at LIMIT 1",
                (message["thread_id"],),
            ).fetchone()
            batch_id = batch["id"] if batch else message_id
            if batch is None:
                self.db.execute(
                    "INSERT INTO message_batches (id,thread_id,status,created_at) VALUES (?,?,'queued',?)",
                    (batch_id, message["thread_id"], time.time()),
                )
            else:
                # Supersede the entire unsent result, including images/voice.
                # Once any send begins the batch is sealed and cannot reopen.
                self.db.execute(
                    "UPDATE outbox SET status='suppressed' WHERE batch_id=? AND status='pending'",
                    (batch_id,),
                )
                self.db.execute(
                    "UPDATE message_batches SET revision=revision+1,status=CASE WHEN status='ready' THEN 'queued' ELSE status END WHERE id=?",
                    (batch_id,),
                )
            self.db.execute(
                "UPDATE inbox SET batch_id=?,status='queued' WHERE message_id=?",
                (batch_id, message_id),
            )
        return batch_id

    def batch(self, batch_id: str) -> dict:
        return dict(
            self.db.execute(
                "SELECT * FROM message_batches WHERE id=?", (batch_id,)
            ).fetchone()
        )

    def batch_messages(self, batch_id: str, pending: bool = False) -> list[dict]:
        return [
            dict(row)
            for row in self.db.execute(
                "SELECT * FROM inbox WHERE batch_id=?"
                + (" AND submitted=0" if pending else "")
                + " ORDER BY received_at,rowid",
                (batch_id,),
            )
        ]

    def batch_routing(self, batch_id: str) -> bool:
        # accept() runs before voice ASR or async session routing. Such an
        # arrival already belongs before the send boundary, even if its media
        # is not yet ready to join/steer the running model turn.
        return (
            self.db.execute(
                "SELECT 1 FROM inbox WHERE thread_id=(SELECT thread_id FROM message_batches WHERE id=?) AND batch_id IS NULL AND status IN ('routing','transcribing') LIMIT 1",
                (batch_id,),
            ).fetchone()
            is not None
        )

    def batch_processing(self, batch_id: str, message_ids: list[str]) -> None:
        with self.db:
            self.db.execute(
                "UPDATE message_batches SET status='processing' WHERE id=?", (batch_id,)
            )
            self.db.executemany(
                "UPDATE inbox SET status='running' WHERE message_id=?",
                [(message_id,) for message_id in message_ids],
            )

    def batch_submitted(self, message_ids: list[str]) -> None:
        with self.db:
            self.db.executemany(
                "UPDATE inbox SET submitted=1 WHERE message_id=?",
                [(message_id,) for message_id in message_ids],
            )

    def stop_batch(self, batch_id: str) -> None:
        with self.db:
            self.db.execute(
                "UPDATE message_batches SET status='stopping' WHERE id=?", (batch_id,)
            )

    def begin_reply(self, reply_id: int) -> bool:
        with self.db:
            item = self.db.execute(
                "SELECT * FROM outbox WHERE id=? AND status='pending'", (reply_id,)
            ).fetchone()
            if item is None:
                return False
            if item["batch_id"]:
                batch = self.batch(item["batch_id"])
                if batch["status"] not in ("ready", "sealed"):
                    return False
                if batch["status"] == "ready" and self.batch_routing(batch["id"]):
                    return False
                # No await between checking arrivals, sealing and claiming.
                # Later messages form a new batch, even during network I/O.
                self.db.execute(
                    "UPDATE message_batches SET status='sealed' WHERE id=?",
                    (batch["id"],),
                )
            self.db.execute(
                "UPDATE outbox SET status='sending' WHERE id=?", (reply_id,)
            )
        return True

    def complete(
        self,
        message_id: str,
        replies: list[str],
        status: str = "done",
        image_path: str | None = None,
        image_paths: list[str] | None = None,
        voice_paths: list[str] | None = None,
        batch_id: str | None = None,
        batch_revision: int | None = None,
    ) -> bool:
        # Commit the result and its outbox entries together. Restarting must
        # neither rerun an already-completed task nor lose its final reply.
        with self.db:
            if batch_id is not None:
                batch = self.batch(batch_id)
                if batch_revision is not None and (
                    batch["revision"] != batch_revision
                    or self.batch_messages(batch_id, pending=True)
                    or self.batch_routing(batch_id)
                ):
                    return False
                message_id = batch_id + (
                    f":r{batch['revision']}" if batch["revision"] else ""
                )
            # Generated images go first, so their delivery outcome is known
            # before sending the model's final text about the generated result.
            for index, path in enumerate(image_paths or []):
                self.db.execute(
                    "INSERT OR IGNORE INTO outbox (dedupe_key,text,kind,media_path,created_at) VALUES (?,?,'image',?,?)",
                    (f"reply:{message_id}:generated:{index}", "", path, time.time()),
                )
            for index, path in enumerate(voice_paths or []):
                self.db.execute(
                    "INSERT OR IGNORE INTO outbox (dedupe_key,text,kind,media_path,created_at) VALUES (?,?,'voice',?,?)",
                    (f"reply:{message_id}:voice:{index}", "", path, time.time()),
                )
            for index, text in enumerate(replies):
                self.db.execute(
                    "INSERT OR IGNORE INTO outbox (dedupe_key,text,created_at) VALUES (?,?,?)",
                    (f"reply:{message_id}:{index}", text, time.time()),
                )
            if image_path:
                self.db.execute(
                    "INSERT OR IGNORE INTO outbox (dedupe_key,text,kind,media_path,created_at) VALUES (?,?,'image',?,?)",
                    (f"reply:{message_id}:meme", "", image_path, time.time()),
                )
            self.db.execute(
                "UPDATE inbox SET status=? WHERE message_id=?", (status, message_id)
            )
            if batch_id is not None:
                prefix = f"reply:{message_id}:"
                self.db.execute(
                    "UPDATE outbox SET batch_id=? WHERE substr(dedupe_key,1,?)=?",
                    (batch_id, len(prefix), prefix),
                )
                self.db.execute(
                    "UPDATE inbox SET status=?,submitted=1 WHERE batch_id=?",
                    (status, batch_id),
                )
                self.db.execute(
                    "UPDATE message_batches SET status=? WHERE id=?",
                    ("sealed" if batch["status"] == "stopping" else "ready", batch_id),
                )
        return True

    def enqueue(self, key: str, text: str) -> None:
        with self.db:
            self.db.execute(
                "INSERT OR IGNORE INTO outbox (dedupe_key,text,created_at) VALUES (?,?,?)",
                (key, text, time.time()),
            )

    def enqueue_image(self, key: str, path: str) -> None:
        with self.db:
            self.db.execute(
                "INSERT OR IGNORE INTO outbox (dedupe_key,text,kind,media_path,created_at) VALUES (?,?,'image',?,?)",
                (key, "", path, time.time()),
            )

    def next_reply(self) -> dict | None:
        row = self.db.execute(
            "SELECT * FROM outbox WHERE status='pending' ORDER BY id LIMIT 1"
        ).fetchone()
        return dict(row) if row else None

    def outgoing_counts(self) -> dict[str, int]:
        counts = dict.fromkeys(("pending", "sending", "blocked", "uncertain"), 0)
        for row in self.db.execute(
            "SELECT status,count(*) FROM outbox WHERE status IN ('pending','sending','blocked','uncertain') GROUP BY status"
        ):
            counts[row[0]] = row[1]
        return counts

    def reply_state(self, reply_id: int, status: str, error: str | None = None) -> None:
        with self.db:
            self.db.execute(
                "UPDATE outbox SET status=?,error=? WHERE id=?",
                (status, error, reply_id),
            )

    def unblock(self) -> None:
        with self.db:
            self.db.execute("UPDATE outbox SET status='pending' WHERE status='blocked'")

    def generated_delivery_failed(self, item: dict, status: str) -> None:
        key = item["dedupe_key"]
        if item["kind"] == "image" and ":generated:" in key:
            separator, label, media = ":generated:", "image", "图片"
        elif item["kind"] == "voice" and ":voice:" in key:
            separator, label, media = ":voice:", "voice", "语音"
        else:
            return
        prefix = key.rsplit(separator, 1)[0]
        failure_key = prefix + f":{label}-failure"
        text = (
            f"{media}已生成，但微信发送被阻止。请发一条新消息后重试。"
            if status == "blocked"
            else f"{media}已生成，但微信发送结果无法确认。为避免重复发送，未自动重发。"
        )
        with self.db:
            self.db.execute(
                "UPDATE outbox SET status='suppressed' WHERE substr(dedupe_key,1,?)=? AND dedupe_key!=? AND kind='text' AND status='pending'",
                (len(prefix) + 1, prefix + ":", failure_key),
            )
            self.db.execute(
                "INSERT OR IGNORE INTO outbox (dedupe_key,text,created_at) VALUES (?,?,?)",
                (failure_key, text, time.time()),
            )

    def recover(self) -> int:
        with self.db:
            # A batch may contain both submitted work and a late queued image.
            # Do not replay any of it after losing an executing turn: tools
            # may already have changed files or sent a remote operation.
            self.db.execute(
                "UPDATE inbox SET status='running' WHERE batch_id IN (SELECT id FROM message_batches WHERE status IN ('processing','stopping'))"
            )
            self.db.execute(
                "UPDATE message_batches SET status='interrupted' WHERE status IN ('processing','stopping')"
            )
            # A lost native send result may already have started the original
            # task. Never automatically resend it after a bridge restart.
            self.db.execute(
                "UPDATE desktop_replies SET status='uncertain' WHERE status='sending'"
            )
            self.db.execute(
                "UPDATE inbox SET status='queued' WHERE status IN ('routing','transcribing')"
            )
            count = self.db.execute(
                "UPDATE inbox SET status='interrupted' WHERE status='running'"
            ).rowcount
            # A crashed send may already have reached WeChat. Do not resend it
            # with a fresh SDK client_id and produce a duplicate notification.
            self.db.execute(
                "UPDATE outbox SET status='uncertain' WHERE status='sending'"
            )
        # A crash after uploading an image must not leave its unverified
        # success prose pending for delivery after restart.
        for row in self.db.execute(
            "SELECT * FROM outbox WHERE status IN ('blocked','uncertain') AND ((kind='image' AND dedupe_key LIKE 'reply:%:generated:%') OR (kind='voice' AND dedupe_key LIKE 'reply:%:voice:%'))"
        ).fetchall():
            self.generated_delivery_failed(dict(row), row["status"])
        return count

    def seed_existing(self, messages_db: Path) -> None:
        if self.get("seeded", False):
            return
        if messages_db.exists():
            source = sqlite3.connect(f"{messages_db.as_uri()}?mode=ro", uri=True)
            try:
                for (message_id,) in source.execute(
                    "SELECT message_id FROM messages WHERE direction=1"
                ):
                    if self.accept(str(message_id), "", None):
                        self.mark(str(message_id), "historical")
            finally:
                source.close()
        self.put("seeded", True)

    def close(self) -> None:
        self.db.close()
