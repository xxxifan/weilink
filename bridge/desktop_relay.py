"""Route explicitly addressed WeChat replies through the desktop's native MCP."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from message_format import WECHAT_STYLE, action, card

log = logging.getLogger(__name__)
ALLOWED_TOOLS = {"read_thread", "send_message_to_thread"}


class DesktopUnavailable(RuntimeError):
    pass


def marker(message_id: str) -> str:
    return "wx-" + hashlib.sha256(message_id.encode()).hexdigest()[:20]


def reply_prompt(job: dict) -> str:
    return (
        f"[微信远程消息 {marker(job['id'])}]\n"
        "以下内容由主人通过已绑定的微信 Bot 直接发送，用于继续本桌面会话。"
        "桥接器会将本轮最终答复回传微信，无需重复调用微信通知工具。\n\n"
        + WECHAT_STYLE
        + "\n\n【主人消息】\n"
        + job["prompt"]
    )


class DesktopClient:
    @asynccontextmanager
    async def session(self):
        # Use the installed first-party MCP server and its normal metadata /
        # approval handling, not a second thread/resume or private execution
        # RPC. Discover the current endpoint after desktop restarts. Multiple
        # endpoints are ambiguous, so do not guess which desktop to control.
        pipes = [
            p.name
            for p in Path("//./pipe/").iterdir()
            if p.name.startswith("codex-app-tools-")
        ]
        packages = list(
            (Path.home() / ".codex/plugins/cache/openai-bundled/codex-app-tools").glob(
                "*/server.mjs"
            )
        )
        if len(pipes) != 1 or not packages:
            raise DesktopUnavailable(
                "桌面 Codex 未运行或连接入口不唯一，消息保留待投递。"
            )
        server = max(packages, key=lambda p: p.stat().st_mtime)
        launcher = server.parent / "scripts/launch_codex_app_tools_mcp.cmd"
        params = StdioServerParameters(
            command="cmd.exe",
            args=["/d", "/s", "/c", "call", str(launcher), str(server)],
            env={**os.environ, "CODEX_APP_TOOLS_PIPE_PATH": "\\\\.\\pipe\\" + pipes[0]},
        )
        async with stdio_client(params) as (reader, writer):
            async with ClientSession(reader, writer) as session:
                await asyncio.wait_for(session.initialize(), 15)
                yield session

    async def call(self, session, name: str, arguments: dict, caller: str) -> dict:
        if name not in ALLOWED_TOOLS:
            raise ValueError("Unsupported desktop relay operation")
        result = await asyncio.wait_for(
            session.call_tool(name, arguments, meta={"threadId": caller}), 30
        )
        text = "\n".join(item.text for item in result.content if item.type == "text")
        if result.isError:
            raise DesktopUnavailable(text[:300] or "桌面工具拒绝了此次调用。")
        try:
            value = json.loads(text)
        except ValueError:
            if name == "send_message_to_thread":
                return {"message": text}
            raise DesktopUnavailable("桌面工具返回了无法识别的数据。") from None
        if not isinstance(value, dict):
            raise DesktopUnavailable("桌面工具返回了无法识别的数据。")
        return value


class DesktopRelay:
    def __init__(self, state, task_monitor, client=None):
        self.state = state
        self.task_monitor = task_monitor
        self.client = client or DesktopClient()
        self.error = None

    def status_text(self) -> str:
        rows = self.state.db.execute(
            "SELECT * FROM desktop_replies ORDER BY created_at DESC LIMIT 12"
        ).fetchall()
        labels = {
            "queued": "等待任务空闲或桌面连接",
            "sending": "正在投递",
            "sent": "已投递，等待原任务回复",
            "uncertain": "投递结果不明，不自动重发",
            "done": "本轮已结束，答复已交微信发送队列",
            "cancelled": "已取消",
            "failed": "失败",
        }
        lines = []
        for row in rows:
            lines.append(
                f"r{row['id']} → {self.state.task_label(row['thread_id'])}「{row['title']}」\n{labels.get(row['status'], row['status'])}"
            )
        if not rows:
            lines.append(action("暂无消息，向原桌面会话发送", "/reply t编号 内容"))
        if self.error:
            lines.append(f"连接提示：{self.error}")
        return card(
            "桌面消息 · 最近 12 条",
            "\n\n".join(lines),
            action("取消尚未投递的消息", "/cancel r编号"),
        )

    def cancel(self, reference: str) -> str:
        if not reference.startswith("r"):
            return (
                action("查看消息编号", "/replies")
                + "\n\n"
                + action("取消尚未投递的消息", "/cancel r编号")
            )
        with self.state.db:
            changed = self.state.db.execute(
                "UPDATE desktop_replies SET status='cancelled' WHERE id=? AND status='queued'",
                (reference[1:],),
            ).rowcount
        return (
            "已取消尚未投递的消息。"
            if changed
            else "消息不存在或已经开始投递，未停止原任务。"
        )

    @staticmethod
    def matched_turn(view: dict, job: dict) -> dict | None:
        needle = f"[微信远程消息 {marker(job['id'])}]"
        for turn in view.get("turns", []):
            for item in turn.get("items", []):
                if item.get("type") == "userMessage" and any(
                    part.get("type") == "text"
                    and part.get("text", "").startswith(needle)
                    for part in item.get("content", [])
                ):
                    return turn
                # The desktop's native send tool records cross-chat input as
                # a codex_app functionCallOutput, not a userMessage. Inspect
                # only that wrapper for correlation; never forward its raw
                # output (or any other tool output) back to WeChat.
                output = item.get("output") or {}
                text = output.get("text", "") if isinstance(output, dict) else ""
                if (
                    item.get("type") == "functionCallOutput"
                    and item.get("namespace") == "codex_app"
                    and item.get("name") == "send_message_to_thread"
                    and f"<source_thread_id>{job['caller_thread_id']}</source_thread_id>"
                    in text
                    and f"<input>{needle}" in text
                ):
                    return turn
        return None

    def finish(self, job: dict, turn: dict) -> None:
        status = turn.get("status")
        if status not in ("completed", "interrupted", "failed"):
            return
        answers = [
            item.get("text", "")
            for item in turn.get("items", [])
            if item.get("type") == "agentMessage"
            and item.get("phase") in (None, "final_answer")
        ]
        text = "\n\n".join(answer for answer in answers if answer.strip())
        outcome = {
            "completed": "本轮已结束",
            "interrupted": "本轮已中断",
            "failed": "本轮失败",
        }[status]
        label = self.state.task_label(job["thread_id"])
        # Atomically save all UTF-8 result parts with job completion. Hidden
        # reasoning and raw tool output never enter the WeChat transcript.
        from transport import split_text

        self.state.desktop_complete(
            job["id"],
            split_text(
                card(
                    f"桌面任务回复 · {label}",
                    f"{job['title']}\n\n{outcome}",
                    text or "没有返回公开文字答复。",
                )
            ),
        )

    async def step(self) -> None:
        jobs = self.state.desktop_jobs()
        if not jobs:
            return
        async with self.client.session() as session:
            busy_targets = set()
            for job in jobs:
                target, caller = job["thread_id"], job["caller_thread_id"]
                if target in busy_targets:
                    continue
                # Recheck ownership and archival on every send; cached task
                # aliases must not let /reply address a bridge-owned thread.
                excluded = {row["thread_id"] for row in self.state.threads()}
                if not any(row["thread_id"] == caller for row in self.state.threads()):
                    raise DesktopUnavailable("微信来源会话不存在，未投递。")
                try:
                    self.task_monitor.find(target, excluded)
                except ValueError:
                    self.state.desktop_complete(
                        job["id"],
                        [
                            f"给 {self.state.task_label(target)} 的消息无法继续处理：任务已归档、不存在或不允许作为目标。"
                        ],
                        "failed",
                    )
                    continue
                view = await self.client.call(
                    session,
                    "read_thread",
                    {
                        "threadId": target,
                        "hostId": "local",
                        "turnLimit": 10,
                        "includeOutputs": True,
                        "maxOutputCharsPerItem": 2048,
                    },
                    caller,
                )
                thread = view.get("thread", {})
                if (
                    thread.get("id") != target
                    or thread.get("kind") != "codex"
                    or thread.get("hostId") != "local"
                ):
                    raise DesktopUnavailable("目标不是本机 Codex 会话，未投递。")
                matched = self.matched_turn(view, job)
                if matched:
                    # Recover an ambiguous send by finding its unique user
                    # message in the original task, never by retransmitting.
                    self.state.desktop_state(job["id"], "sent")
                    self.finish(job, matched)
                    if matched.get("status") not in (
                        "completed",
                        "interrupted",
                        "failed",
                    ):
                        busy_targets.add(target)
                    continue
                if job["status"] != "queued":
                    busy_targets.add(target)
                    continue
                status = thread.get("status")
                status = status.get("type") if isinstance(status, dict) else status
                turns = view.get("turns", [])
                if status not in ("idle", "notLoaded") or any(
                    t.get("status") == "inProgress" for t in turns
                ):
                    busy_targets.add(target)
                    continue
                self.state.desktop_state(job["id"], "sending")
                try:
                    receipt = await self.client.call(
                        session,
                        "send_message_to_thread",
                        {
                            "threadId": target,
                            "hostId": "local",
                            "prompt": reply_prompt(job),
                        },
                        caller,
                    )
                    if receipt.get("threadId") != target:
                        raise DesktopUnavailable(
                            "桌面端未确认目标会话，投递结果待检查。"
                        )
                except Exception as exc:
                    self.state.desktop_state(job["id"], "uncertain", type(exc).__name__)
                    self.state.enqueue(
                        f"desktop-uncertain:{job['id']}",
                        card(
                            f"投递结果待确认 · {self.state.task_label(target)}",
                            "未自动重发，请在桌面端检查，或查看消息状态。",
                            action("查看消息状态", "/replies"),
                        ),
                    )
                    raise
                self.state.desktop_state(job["id"], "sent")
                self.state.enqueue(
                    f"desktop-sent:{job['id']}",
                    card(f"消息已投递 · {self.state.task_label(target)}", job["title"]),
                )
                busy_targets.add(target)
        self.error = None

    async def run(self, stopping: asyncio.Event) -> None:
        while not stopping.is_set():
            try:
                await self.step()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.error = (
                    str(exc)[:300]
                    if isinstance(exc, DesktopUnavailable)
                    else type(exc).__name__
                )
                log.warning("Desktop relay deferred: %s", type(exc).__name__)
            try:
                await asyncio.wait_for(stopping.wait(), 5)
            except TimeoutError:
                pass
