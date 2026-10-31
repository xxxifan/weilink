"""Send a requested task notification to the already-authorized owner."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
import uuid
from pathlib import Path

from message_format import action, card
from state import State
from task_monitor import TaskMonitor
from transport import ROOT, load_config, send_text


def prepare_notification(
    config: dict, text: str, thread_id: str | None, state: State, monitor=None
) -> dict:
    result = {
        "to": config["owner_user_id"],
        "bot_id": config["bot_id"],
        "text": text,
        "sends_message": False,
    }
    if not thread_id:
        result["routing"] = "unbound"
        return result
    owned = {row["thread_id"] for row in state.threads()}
    if thread_id in owned:
        result["routing"] = "wechat-conversation"
        return result
    task = (monitor or TaskMonitor()).find(thread_id, owned)
    label = state.register_origin(task["id"], task["title"])
    result.update(thread_id=task["id"], task_alias=label, routing="desktop")
    result["text"] += "\n\n" + card(
        f"原桌面会话 · {label}",
        task["title"],
        action("继续此任务", f"/reply {label} 消息内容"),
    )
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", action="store_true")
    parser.add_argument("--text-file", type=Path)
    parser.add_argument(
        "--id", help="Stable request ID; reuse it when checking the same notification"
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--prepare",
        action="store_true",
        help="Register source and return the exact MCP notification text without sending",
    )
    parser.add_argument(
        "--thread-id",
        help="Source desktop task ID; defaults to the current Codex thread environment",
    )
    args = parser.parse_args()
    config = load_config()
    if args.target:
        print(json.dumps({"to": config["owner_user_id"], "bot_id": config["bot_id"]}))
        return
    if not args.text_file:
        parser.error("--text-file is required unless --target is used")
    text = args.text_file.read_text(encoding="utf-8-sig").strip()
    if not text or len(text.encode("utf-8")) > 12000:
        raise SystemExit("Notification must contain 1 to 12000 UTF-8 bytes")
    if args.dry_run:
        print(
            json.dumps(
                {
                    "valid": True,
                    "bytes": len(text.encode("utf-8")),
                    "sends_message": False,
                }
            )
        )
        return
    source = (
        args.thread_id
        or os.environ.get("CODEX_THREAD_ID")
        or os.environ.get("CODEX_SESSION_ID")
    )
    state = State(ROOT / "bridge-state.sqlite")
    try:
        prepared = prepare_notification(config, text, source, state)
    finally:
        state.close()
    text = prepared["text"]
    if args.prepare:
        print(json.dumps(prepared, ensure_ascii=False))
        return
    request_id = args.id or str(uuid.uuid4())
    key = hashlib.sha256(request_id.encode()).hexdigest()
    directory = ROOT / "notification-receipts"
    directory.mkdir(exist_ok=True)
    path = directory / f"{key}.json"
    if path.exists():
        print(path.read_text(encoding="utf-8"))
        return
    receipt = {
        "request_id": request_id,
        "status": "sending",
        "created_at": time.time(),
        "thread_id": prepared.get("thread_id"),
        "task_alias": prepared.get("task_alias"),
    }
    # Exclusive creation also prevents two notification callers racing to send
    # the same event. Never retry a previously uncertain network outcome.
    try:
        with path.open("x", encoding="utf-8") as handle:
            json.dump(receipt, handle)
    except FileExistsError:
        print(path.read_text(encoding="utf-8"))
        return
    try:
        receipt["status"] = "sent" if send_text(config, text) else "blocked"
    except Exception as exc:
        receipt.update(status="uncertain", error=type(exc).__name__)
    path.write_text(json.dumps(receipt), encoding="utf-8")
    print(json.dumps(receipt))
    if receipt["status"] != "sent":
        sys.exit(1)


if __name__ == "__main__":
    main()
