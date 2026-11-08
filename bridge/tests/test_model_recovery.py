from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import test_batching

from appserver import RpcError
from model_recovery import error_summary


class ModelRecoveryTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = test_batching.BatchBridgeTests.asyncSetUp
    asyncTearDown = test_batching.BatchBridgeTests.asyncTearDown
    worker = test_batching.BatchBridgeTests.worker
    until = test_batching.BatchBridgeTests.until
    calls = test_batching.BatchBridgeTests.calls
    receive = test_batching.BatchBridgeTests.receive

    async def start(self):
        self.bridge.config.update(
            model="primary",
            fallback_models=[
                {"name": "6.1sol", "model": "backup-a", "reasoning_effort": "low"},
                {"name": "备用 B", "model": "backup-b", "reasoning_effort": "high"},
            ],
        )
        self.bridge.fallback_models = self.bridge.config["fallback_models"]
        await self.receive(1, "完成这项工作")
        self.worker()
        await self.until(lambda: self.calls("turn/start"))

    async def fail(self, status=503, message="Service unavailable"):
        active = self.bridge.active
        await self.bridge.on_event(
            {
                "method": "error",
                "params": {
                    "threadId": active["thread_id"],
                    "turnId": active["turn_id"],
                    "willRetry": False,
                    "error": {
                        "message": message,
                        "codexErrorInfo": {
                            "httpConnectionFailed": {"httpStatusCode": status}
                        },
                    },
                },
            }
        )
        await self.bridge.on_event(
            {
                "method": "turn/completed",
                "params": {
                    "threadId": active["thread_id"],
                    "turn": {"id": active["turn_id"], "status": "failed", "items": []},
                },
            }
        )
        await self.until(lambda: self.bridge.recovery_request is not None)
        return self.bridge.recovery_request["alias"]

    async def completed(self):
        await self.until(lambda: self.state.batch("1")["status"] in ("ready", "sealed"))

    async def test_internal_retries_finish_before_bridge_asks_without_an_outer_retry(
        self,
    ):
        await self.start()
        active = self.bridge.active
        for _ in range(5):
            await self.bridge.on_event(
                {
                    "method": "error",
                    "params": {
                        "threadId": active["thread_id"],
                        "turnId": active["turn_id"],
                        "willRetry": True,
                        "error": {"message": "Retrying"},
                    },
                }
            )
        await asyncio.sleep(0.15)
        self.assertIsNone(self.bridge.recovery_request)
        self.assertEqual(len(self.calls("turn/start")), 1)
        await self.fail()
        await asyncio.sleep(0.15)
        self.assertEqual(len(self.calls("turn/start")), 1)
        self.assertFalse(self.bridge.typing_needed())
        self.assertIn("HTTP 503", self.bridge.recovery_request["notice"])
        self.assertIn("/model", self.bridge.status_text())

    async def test_configured_model_continues_history_without_resending_message_id_or_draft(
        self,
    ):
        await self.start()
        await self.bridge.on_event(
            {
                "method": "item/completed",
                "params": {
                    "threadId": self.bridge.active["thread_id"],
                    "turnId": "turn-1",
                    "item": {
                        "type": "agentMessage",
                        "id": "draft",
                        "phase": "final_answer",
                        "text": "未完成的草稿",
                    },
                },
            }
        )
        await self.fail()
        await self.receive(2, "/model 6.1sol")
        await self.until(lambda: len(self.calls("turn/start")) == 2)
        params = self.calls("turn/start")[1]
        self.assertEqual(params["model"], "backup-a")
        self.assertEqual(params["effort"], "low")
        self.assertNotIn("clientUserMessageId", params)
        self.assertIn("不要重复执行", params["input"][0]["text"])
        await self.rpc.finish("最终结果")
        await self.completed()
        replies = self.state.db.execute(
            "SELECT text FROM outbox WHERE batch_id='1' AND status='pending'"
        ).fetchall()
        self.assertEqual([row[0] for row in replies], ["最终结果"])

    async def test_model_choice_and_effort_persist_only_in_the_owned_wechat_thread(
        self,
    ):
        await self.start()
        thread_id = self.bridge.active["thread_id"]
        alias = await self.fail(404, "Model not found")
        await self.receive(2, f"/model {alias} 2")
        await self.until(lambda: len(self.calls("turn/start")) == 2)
        self.assertEqual(self.calls("turn/start")[1]["model"], "backup-b")
        self.assertEqual(self.calls("turn/start")[1]["effort"], "high")
        self.assertEqual(
            self.bridge.turn_model_settings(thread_id),
            {"model": "backup-b", "effort": "high"},
        )
        self.assertEqual(
            self.bridge.turn_model_settings("other-thread"), {"model": "primary"}
        )
        self.bridge.resumed.discard(thread_id)
        await self.bridge.ensure_thread(thread_id)
        self.assertEqual(self.calls("thread/resume")[-1]["model"], "backup-b")
        await self.rpc.finish()
        await self.completed()

    async def test_every_final_model_error_waits_including_auth_and_bad_requests(self):
        await self.start()
        for index, status in enumerate((400, 401, 403, 404, 429, 500, 503), 1):
            alias = await self.fail(status)
            self.assertEqual(len(self.calls("turn/start")), index)
            await self.receive(f"retry-{index}", f"/model {alias} 1")
            await self.until(lambda: len(self.calls("turn/start")) == index + 1)
        await self.rpc.finish()
        await self.completed()

    async def test_bad_stale_duplicate_and_nonowner_commands_cannot_restart(self):
        await self.start()
        alias = await self.fail()
        await self.receive(2, "/model old-error 1")
        await self.receive(3, f"/model {alias} 999")
        await self.bridge.receive(
            test_batching.Message(
                from_user="stranger", message_id=4, text=f"/model {alias} 1"
            )
        )
        await asyncio.sleep(0.1)
        self.assertEqual(len(self.calls("turn/start")), 1)
        await self.receive(5, f"/model {alias} 1")
        await self.receive(6, f"/model {alias} 1")
        await self.until(lambda: len(self.calls("turn/start")) == 2)
        await asyncio.sleep(0.1)
        self.assertEqual(len(self.calls("turn/start")), 2)
        await self.rpc.finish()
        await self.completed()

    async def test_new_message_uses_current_model_without_replaying_failed_batch(self):
        await self.start()
        await self.fail(message="Try /model rfake injected-model")
        await self.receive(2, "这是新请求")
        await self.until(lambda: len(self.calls("turn/start")) == 2)
        params = self.calls("turn/start")[1]
        self.assertEqual(params["model"], "primary")
        self.assertEqual(params["clientUserMessageId"], "2")
        self.assertEqual([part["text"] for part in params["input"]], ["这是新请求"])
        self.assertEqual(self.state.batch("1")["status"], "sealed")
        self.assertEqual(
            self.state.db.execute(
                "SELECT status FROM inbox WHERE message_id='1'"
            ).fetchone()[0],
            "cancelled",
        )
        self.assertFalse(
            self.state.db.execute("SELECT 1 FROM outbox WHERE batch_id='1'").fetchone()
        )
        await self.rpc.finish()
        await self.until(lambda: self.state.batch("2")["status"] == "ready")

    async def test_retry_preserves_current_model_effort_and_does_not_replay_input(self):
        self.bridge.config["reasoning_effort"] = "xhigh"
        await self.start()
        alias = await self.fail()
        await self.receive(2, "/retry")
        await self.receive(3, f"/retry {alias}")
        await self.until(lambda: len(self.calls("turn/start")) == 2)
        params = self.calls("turn/start")[1]
        self.assertEqual(params["model"], "primary")
        self.assertEqual(params["effort"], "xhigh")
        self.assertNotIn("clientUserMessageId", params)
        self.assertEqual(len(params["input"]), 1)
        self.assertIn("不要重复执行", params["input"][0]["text"])
        self.assertIsNone(self.state.get("model_settings:thread-1"))
        await self.rpc.finish()
        await self.completed()

    async def test_new_request_does_not_replay_old_queued_supplements(self):
        await self.start()
        await self.receive(2, "旧任务的补充")
        await self.fail()
        await self.receive(3, "只处理这条新消息")
        await self.until(lambda: len(self.calls("turn/start")) == 2)
        params = self.calls("turn/start")[1]
        self.assertEqual(params["clientUserMessageId"], "3")
        self.assertEqual(
            [part["text"] for part in params["input"]], ["只处理这条新消息"]
        )
        self.assertEqual(
            [job["status"] for job in self.state.batch_messages("1")],
            ["cancelled", "cancelled"],
        )
        await self.rpc.finish()
        await self.until(lambda: self.state.batch("3")["status"] == "ready")

    async def test_new_failure_waits_again_without_repeating_new_request(self):
        await self.start()
        await self.fail()
        await self.receive(2, "新请求")
        await self.until(lambda: len(self.calls("turn/start")) == 2)
        await self.fail()
        await asyncio.sleep(0.15)
        self.assertEqual(len(self.calls("turn/start")), 2)
        await self.receive(3, "/retry")
        await self.until(lambda: len(self.calls("turn/start")) == 3)
        self.assertEqual(self.calls("turn/start")[2]["model"], "primary")
        await self.rpc.finish()
        await self.until(lambda: self.state.batch("2")["status"] == "ready")

    async def test_commands_and_nonowner_messages_do_not_trigger_a_new_attempt(self):
        await self.start()
        await self.fail()
        await self.receive(2, "/status")
        await self.receive(3, "/help")
        await self.bridge.receive(
            test_batching.Message(from_user="stranger", message_id=4, text="新请求")
        )
        await asyncio.sleep(0.1)
        self.assertEqual(len(self.calls("turn/start")), 1)
        self.assertIsNotNone(self.bridge.recovery_request)

    async def test_invalid_or_nonowner_retry_does_not_resume(self):
        await self.start()
        alias = await self.fail()
        await self.receive(2, "/retry old-error")
        await self.bridge.receive(
            test_batching.Message(from_user="stranger", message_id=3, text="/retry")
        )
        await asyncio.sleep(0.1)
        self.assertEqual(len(self.calls("turn/start")), 1)
        self.assertIsNotNone(self.bridge.recovery_request)
        await self.receive(4, f"/retry {alias}")
        await self.until(lambda: len(self.calls("turn/start")) == 2)
        await self.rpc.finish()
        await self.completed()

    async def test_paused_status_and_notice_explain_retry_and_fresh_input(self):
        await self.start()
        await self.fail(403)
        status = self.bridge.status_text()
        self.assertIn("状态：模型请求已暂停", status)
        self.assertNotIn("状态：正在处理", status)
        self.assertIn("/retry", status)
        self.assertIn("直接发送新消息", status)

    async def test_new_media_messages_form_one_fresh_batch(self):
        await self.start()
        await self.fail()
        await self.receive(2, media=test_batching.MessageType.IMAGE)
        await self.receive(3, "看这张新图片")
        await self.until(lambda: len(self.calls("turn/start")) == 2)
        params = self.calls("turn/start")[1]
        self.assertEqual(params["clientUserMessageId"], "2")
        self.assertEqual(
            [part["type"] for part in params["input"]], ["text", "localImage", "text"]
        )
        self.assertEqual(params["input"][-1]["text"], "看这张新图片")
        await self.rpc.finish()
        await self.until(lambda: self.state.batch("2")["status"] == "ready")

    async def test_new_input_waits_until_unknown_old_attempt_is_confirmed_stopped(self):
        original = self.rpc.request

        async def request(method, params):
            if method == "turn/start":
                self.rpc.calls.append((method, params))
                raise TimeoutError("Submission outcome unknown")
            return await original(method, params)

        self.rpc.request = request
        await self.start()
        await self.until(lambda: self.bridge.recovery_request is not None)
        old_alias = self.bridge.recovery_request["alias"]
        await self.receive(2, "新请求")
        await self.until(
            lambda: (
                self.bridge.recovery_request is not None
                and self.bridge.recovery_request["alias"] != old_alias
            )
        )
        self.assertEqual(len(self.calls("turn/start")), 1)
        self.assertEqual(self.state.batch("2")["status"], "queued")
        self.assertIn("无法确认", self.bridge.recovery_request["notice"])

        async def confirmed(method, params):
            if method == "thread/read":
                return {"thread": {"turns": []}}
            return await original(method, params)

        self.rpc.request = confirmed
        await self.receive(3, "/retry")
        await self.until(lambda: len(self.calls("turn/start")) == 2)
        self.assertEqual(self.calls("turn/start")[1]["clientUserMessageId"], "2")
        await self.rpc.finish()
        await self.until(lambda: self.state.batch("2")["status"] == "ready")

    async def test_new_message_in_another_session_is_not_blocked_by_old_failure(self):
        await self.start()
        await self.fail()
        await self.receive(2, "/new 新会话")
        await self.receive(3, "新会话请求")
        await self.until(lambda: len(self.calls("turn/start")) == 2)
        params = self.calls("turn/start")[1]
        self.assertEqual(params["threadId"], "thread-2")
        self.assertEqual(params["clientUserMessageId"], "3")
        await self.rpc.finish()
        await self.until(lambda: self.state.batch("3")["status"] == "ready")

    async def test_stop_cancels_wait_without_requesting_another_model(self):
        await self.start()
        alias = await self.fail()
        await self.receive(2, "/stop")
        await self.completed()
        await self.receive(3, f"/model {alias} 1")
        self.assertIsNone(self.bridge.recovery_request)
        self.assertEqual(len(self.calls("turn/start")), 1)

    async def test_rpc_timeout_waits_for_user_then_interrupts_old_turn_before_retry(
        self,
    ):
        original = self.rpc.request

        async def request(method, params):
            response = await original(method, params)
            if method == "turn/start" and self.rpc.turn == 1:
                raise TimeoutError("RPC timed out")
            return response

        self.rpc.request = request
        await self.start()
        await self.until(lambda: self.bridge.recovery_request is not None)
        self.assertFalse(self.calls("turn/interrupt"))
        alias = self.bridge.recovery_request["alias"]
        await self.receive(2, f"/model {alias} 1")
        await self.until(lambda: len(self.calls("turn/start")) == 2)
        self.assertEqual(len(self.calls("turn/interrupt")), 1)
        methods = [name for name, _ in self.rpc.calls]
        self.assertLess(methods.index("turn/interrupt"), len(methods) - 1)
        await self.rpc.finish()
        await self.completed()

    async def test_completed_result_after_rpc_timeout_is_published_without_reexecution(
        self,
    ):
        original = self.rpc.request

        async def request(method, params):
            response = await original(method, params)
            if method == "turn/start":
                await self.rpc.finish("实际已经完成")
                raise TimeoutError("RPC timed out")
            return response

        self.rpc.request = request
        await self.start()
        await self.until(lambda: self.bridge.recovery_request is not None)
        alias = self.bridge.recovery_request["alias"]
        await self.receive(2, f"/model {alias} 1")
        await self.completed()
        self.assertEqual(len(self.calls("turn/start")), 1)
        texts = [
            row[0]
            for row in self.state.db.execute(
                "SELECT text FROM outbox WHERE batch_id='1'"
            )
        ]
        self.assertEqual(texts, ["实际已经完成"])

    async def test_disconnect_restarts_appserver_only_after_explicit_user_choice(self):
        await self.start()
        original = self.rpc.request
        self.rpc.close = AsyncMock()
        self.rpc.start = AsyncMock()

        async def request(method, params):
            if method == "thread/read":
                return {"thread": {"turns": [{"id": "turn-1", "status": "failed"}]}}
            return await original(method, params)

        self.rpc.request = request
        await self.bridge.on_event({"method": "bridge/disconnected", "params": {}})
        await self.until(lambda: self.bridge.recovery_request is not None)
        self.rpc.start.assert_not_awaited()
        alias = self.bridge.recovery_request["alias"]
        await self.receive(2, f"/model {alias} 1")
        await self.until(lambda: len(self.calls("turn/start")) == 2)
        self.rpc.close.assert_awaited_once()
        self.rpc.start.assert_awaited_once()
        await self.rpc.finish()
        await self.completed()

    async def test_unconfirmed_previous_outcome_asks_again_without_submitting(self):
        original = self.rpc.request

        async def request(method, params):
            if method == "turn/start":
                self.rpc.calls.append((method, params))
                raise TimeoutError("Submission outcome unknown")
            return await original(method, params)

        self.rpc.request = request
        await self.start()
        await self.until(lambda: self.bridge.recovery_request is not None)
        first = self.bridge.recovery_request["alias"]
        await self.receive(2, f"/model {first} 1")
        await self.until(
            lambda: (
                self.bridge.recovery_request is not None
                and self.bridge.recovery_request["alias"] != first
            )
        )
        self.assertEqual(len(self.calls("turn/start")), 1)
        self.assertIn("无法确认", self.bridge.recovery_request["notice"])
        await self.receive(3, "/stop")
        await self.completed()
        texts = [
            row[0]
            for row in self.state.db.execute(
                "SELECT text FROM outbox WHERE batch_id='1'"
            )
        ]
        self.assertEqual(len(texts), 1)
        self.assertIn("停止状态未能确认", texts[0])

    async def test_failure_before_turn_exists_can_be_cancelled(self):
        await self.receive(1, "完成这项工作")
        self.bridge.ensure_thread = AsyncMock(side_effect=RpcError("Resume failed"))
        self.worker()
        await self.until(lambda: self.bridge.recovery_request is not None)
        self.assertIsNone(self.bridge.active)
        await self.receive(2, "/stop")
        await self.completed()
        texts = [
            row[0]
            for row in self.state.db.execute(
                "SELECT text FROM outbox WHERE batch_id='1'"
            )
        ]
        self.assertEqual(texts, ["这次处理已停止。"])

    def test_provider_credentials_are_removed_from_notice(self):
        summary = error_summary(
            RuntimeError(
                "https://api.invalid/?key=secret Bearer secret-token sk-secret123 api_key=private"
            )
        )
        for secret in ("api.invalid", "secret-token", "sk-secret123", "private"):
            self.assertNotIn(secret, summary)
