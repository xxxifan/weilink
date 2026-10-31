"""Persistent exact-ID quotation lookup for both sides of a WeChat conversation."""

from __future__ import annotations

import json
import sqlite3
import time
from contextlib import closing, contextmanager
from pathlib import Path


def message_key(value) -> str | None:
    # Keep server IDs as strings: they can exceed both JS and SQLite integer
    # ranges. Never match by nearest time, reply order, or similar wording.
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        return None
    text = str(value).strip()
    return (
        text
        if text and len(text) <= 128 and text.isascii() and text.isdigit()
        else None
    )


class QuoteStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("""CREATE TABLE IF NOT EXISTS quotes (
                bot_id TEXT NOT NULL, peer_id TEXT NOT NULL,
                message_id TEXT NOT NULL, content TEXT NOT NULL,
                created_at REAL NOT NULL,
                PRIMARY KEY (bot_id,peer_id,message_id))""")
            db.execute("CREATE INDEX IF NOT EXISTS quotes_age ON quotes(created_at)")

    @contextmanager
    def connect(self):
        # Sender and receiver run on separate threads and use separate SDK
        # handles; short-lived connections also allow bridge restarts safely.
        with closing(sqlite3.connect(self.path, timeout=5)) as db, db:
            yield db

    def put(self, bot: str, peer: str, message_id, content: dict) -> None:
        key = message_key(message_id)
        if not key or not bot or not peer:
            return
        with self.connect() as db:
            db.execute(
                "INSERT OR REPLACE INTO quotes VALUES (?,?,?,?,?)",
                (bot, peer, key, json.dumps(content, ensure_ascii=False), time.time()),
            )
            db.execute(
                "DELETE FROM quotes WHERE created_at<?", (time.time() - 30 * 86400,)
            )
            db.execute("""DELETE FROM quotes WHERE rowid IN (
                SELECT rowid FROM quotes ORDER BY created_at DESC LIMIT -1 OFFSET 10000)""")

    def get(self, bot: str, peer: str, message_id) -> dict | None:
        key = message_key(message_id)
        if not key or not bot or not peer:
            return None
        with self.connect() as db:
            row = db.execute(
                "SELECT content FROM quotes WHERE bot_id=? AND peer_id=? AND message_id=? AND created_at>=?",
                (bot, peer, key, time.time() - 30 * 86400),
            ).fetchone()
        return json.loads(row[0]) if row else None
