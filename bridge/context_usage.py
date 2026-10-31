"""Normalize usage reports; optionally read a bounded tail of an owned rollout."""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path

FIELDS = (
    "totalTokens",
    "inputTokens",
    "outputTokens",
    "cachedInputTokens",
    "cacheWriteInputTokens",
    "reasoningOutputTokens",
)
SAVED_FIELDS = (
    "total_tokens",
    "input_tokens",
    "output_tokens",
    "cached_input_tokens",
    "cache_write_input_tokens",
    "reasoning_output_tokens",
)


def normalize_usage(raw: dict, reported_at: float | None, source: str) -> dict | None:
    if not isinstance(raw, dict):
        return None
    result = {"reported_at": reported_at, "source": source}
    for group in ("last", "total"):
        breakdown = raw.get(group)
        if not isinstance(breakdown, dict):
            return None
        values = {
            field: value
            for field in FIELDS
            if type(value := breakdown.get(field)) is int and value >= 0
        }
        if "totalTokens" not in values:
            return None
        result[group] = values
    window = raw.get("modelContextWindow")
    result["modelContextWindow"] = (
        window if type(window) is int and window > 0 else None
    )
    return result


def saved_usage(thread_id: str, codex_home: Path | None = None) -> dict | None:
    home = codex_home or Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
    try:
        with closing(
            sqlite3.connect(
                (home / "state_5.sqlite").resolve().as_uri() + "?mode=ro",
                uri=True,
                timeout=0.5,
            )
        ) as db:
            db.execute("PRAGMA query_only=ON")
            row = db.execute(
                "SELECT rollout_path FROM threads WHERE id=?", (thread_id,)
            ).fetchone()
        if not row:
            return None
        path = Path(row[0])
        # Never hydrate/resume a thread just to obtain usage. This fallback
        # reads at most 1 MiB, only for a bridge-owned conversation. Missing,
        # compressed, or incompatible history is explicitly treated as unknown.
        size = path.stat().st_size
        with path.open("rb") as stream:
            stream.seek(max(0, size - 1024 * 1024))
            lines = stream.read(1024 * 1024).splitlines()
        for line in reversed(lines):
            try:
                event = json.loads(line)
                payload = event.get("payload", {})
                if (
                    event.get("type") != "event_msg"
                    or payload.get("type") != "token_count"
                ):
                    continue
                info = payload.get("info")
                if not isinstance(info, dict):
                    continue
                raw = {"modelContextWindow": info.get("model_context_window")}
                for group, key in (
                    ("last", "last_token_usage"),
                    ("total", "total_token_usage"),
                ):
                    value = info.get(key) or {}
                    raw[group] = {
                        camel: value.get(snake)
                        for camel, snake in zip(FIELDS, SAVED_FIELDS, strict=True)
                    }
                stamp = event.get("timestamp")
                reported_at = (
                    datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()
                    if stamp
                    else None
                )
                result = normalize_usage(raw, reported_at, "本机已保存记录")
                if result is not None:
                    return result
            except (ValueError, TypeError, AttributeError, OverflowError):
                continue
    except (OSError, sqlite3.Error):
        pass
    return None


def context_summary(usage: dict | None) -> str:
    if usage is None:
        return "上下文占用：暂无用量报告（并非 0；模型回复后更新）"
    # Session totals include repeated requests and can grow beyond the context
    # window. Use only the last request total; cached input still occupies
    # context and reasoning output is already included in output/total.
    used = usage["last"]["totalTokens"]
    window = usage.get("modelContextWindow")
    if window:
        return f"上下文占用估算（最近报告）：{used:,} / {window:,} tokens（{used / window:.1%}）"
    return f"上下文占用估算（最近报告）：{used:,} tokens；窗口上限未报告"
