from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import threading
import unittest
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from weilink import _protocol as proto

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from delivery_status import BASE_INFO, START, STOP, DeliveryStatus, StatusTransport
from state import State
from wechat_bridge import Bridge


class StatusTransportTests(unittest.TestCase):
    def setUp(self):
        self.now = 0.0
        self.transport = StatusTransport(
            {"owner_user_id": "owner", "data_path": "unused"},
            clock=lambda: self.now,
        )
        self.info = SimpleNamespace(
            bot_id="bot", token="secret-token", base_url="https://api.invalid/"
        )
        self.session = SimpleNamespace(
            bot_info=self.info, context_tokens={"owner": "context"}
        )
        self.client = Mock()
        self.client._find_session_for_user.return_value = self.session
        self.client._sessions = {"main": self.session}

    def test_typing_cache_cancel_refresh_and_account_notifications(self):
        with (
            patch("delivery_status.WeiLink", return_value=self.client),
            patch.object(
                self.transport,
                "_request",
                side_effect=lambda info, endpoint, body: (
                    {"typing_ticket": "ticket"}
                    if endpoint == proto.EP_GET_CONFIG
                    else {"ret": 0}
                ),
            ) as request,
        ):
            self.assertTrue(self.transport.notify(True))
            self.assertTrue(self.transport.typing(True))
            self.assertTrue(self.transport.typing(True))
            self.assertTrue(self.transport.typing(False))
            self.now = 3601
            self.assertTrue(self.transport.typing(True))
            self.assertTrue(self.transport.notify(False))
            calls = [(call.args[1], call.args[2]) for call in request.call_args_list]
            self.assertEqual(
                [endpoint for endpoint, _ in calls],
                [
                    START,
                    proto.EP_GET_CONFIG,
                    proto.EP_SEND_TYPING,
                    proto.EP_SEND_TYPING,
                    proto.EP_SEND_TYPING,
                    proto.EP_GET_CONFIG,
                    proto.EP_SEND_TYPING,
                    STOP,
                ],
            )
            self.assertEqual(
                calls[1][1], {"ilink_user_id": "owner", "context_token": "context"}
            )
            self.assertEqual(
                [
                    body["status"]
                    for endpoint, body in calls
                    if endpoint == proto.EP_SEND_TYPING
                ],
                [1, 1, 2, 1],
            )
            self.assertEqual(self.client.close.call_count, 6)
            self.client.send.assert_not_called()
            self.client.recv.assert_not_called()

    def test_missing_ticket_is_not_reported_as_success_or_sent(self):
        with (
            patch("delivery_status.WeiLink", return_value=self.client),
            patch.object(
                self.transport,
                "_request",
                return_value={"ret": 0},
            ) as request,
        ):
            self.assertFalse(self.transport.typing(True))
            self.assertEqual(request.call_count, 1)
            self.assertFalse(self.transport.tickets)
            self.client.close.assert_called_once()

    def test_rejected_ticket_and_relogin_force_config_refresh(self):
        def respond(info, endpoint, body):
            return (
                {"typing_ticket": "ticket"} if endpoint == proto.EP_GET_CONFIG else {}
            )

        with (
            patch("delivery_status.WeiLink", return_value=self.client),
            patch.object(
                self.transport,
                "_request",
                side_effect=respond,
            ) as request,
        ):
            self.transport.typing(True)
            request.side_effect = proto.ILinkError(ret=-1)
            with self.assertRaises(proto.ILinkError):
                self.transport.typing(True)
            self.assertFalse(self.transport.tickets)
            request.side_effect = respond
            self.transport.typing(True)
            self.info.token = "renewed-secret"
            self.transport.typing(True)
            configs = [
                call
                for call in request.call_args_list
                if call.args[1] == proto.EP_GET_CONFIG
            ]
            self.assertEqual(len(configs), 3)

    def test_no_context_only_falls_back_to_a_single_logged_in_account(self):
        self.client._find_session_for_user.return_value = None
        with (
            patch("delivery_status.WeiLink", return_value=self.client),
            patch.object(
                self.transport,
                "_request",
                return_value={"ret": 0},
            ) as request,
        ):
            self.assertTrue(self.transport.notify(True))
            request.reset_mock()
            self.client._sessions["second"] = self.session
            self.assertFalse(self.transport.notify(True))
            self.assertFalse(self.transport.typing(True))
            request.assert_not_called()

    def test_wire_format_empty_typing_success_and_business_error_checks(self):
        for endpoint, body, response in (
            (START, {}, b'{"ret":0}'),
            (STOP, {}, b'{"ret":0}'),
            (
                proto.EP_SEND_TYPING,
                {"ilink_user_id": "owner", "typing_ticket": "ticket", "status": 2},
                b"",
            ),
        ):
            with (
                self.subTest(endpoint=endpoint),
                patch(
                    "delivery_status.urllib.request.urlopen",
                    return_value=BytesIO(response),
                ) as urlopen,
            ):
                self.transport._request(self.info, endpoint, body)
                request = urlopen.call_args.args[0]
                self.assertEqual(request.full_url, "https://api.invalid" + endpoint)
                self.assertEqual(
                    json.loads(request.data), {**body, "base_info": BASE_INFO}
                )
                self.assertEqual(
                    request.get_header("Authorization"), "Bearer secret-token"
                )
                self.assertEqual(urlopen.call_args.kwargs["timeout"], 5)
        for response in (
            b'{"ret":1}',
            b'{"ret":0,"errcode":8}',
            b'{"ret":0,"errcode":-14}',
        ):
            with patch(
                "delivery_status.urllib.request.urlopen", return_value=BytesIO(response)
            ):
                with self.assertRaises(proto.ILinkError):
                    self.transport._request(self.info, START, {})
        with patch("delivery_status.urllib.request.urlopen", return_value=BytesIO(b"")):
            with self.assertRaises(ValueError):
                self.transport._request(self.info, START, {})


class DeliveryStatusTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.now = 0.0
        self.transport = Mock()
        self.transport.notify.return_value = True
        self.transport.typing.return_value = True
        self.delivery = DeliveryStatus(self.transport, clock=lambda: self.now)

    async def test_idle_cancel_start_keepalive_and_cancel_on_transition(self):
        await self.delivery.step(False)
        await self.delivery.step(False)
        await self.delivery.step(True)
        self.now = 4.9
        await self.delivery.step(True)
        self.now = 5
        await self.delivery.step(True)
        await self.delivery.step(False)
        self.now = 100
        await self.delivery.step(False)
        self.assertEqual(
            [call.args[0] for call in self.transport.typing.call_args_list],
            [False, True, True, False],
        )
        self.transport.notify.assert_called_once_with(True)
        self.assertFalse(self.delivery.typing)

    async def test_failure_backoff_redaction_and_immediate_cancel(self):
        self.transport.notify.side_effect = TimeoutError("secret-token")
        self.transport.typing.side_effect = TimeoutError("ticket-secret")
        await self.delivery.step(True)
        self.now = 4
        await self.delivery.step(True)
        self.assertEqual(self.transport.typing.call_count, 1)
        self.now = 5
        await self.delivery.step(True)
        self.now = 14
        await self.delivery.step(True)
        self.assertEqual(self.transport.typing.call_count, 2)
        self.transport.typing.side_effect = None
        await self.delivery.step(False)
        self.assertFalse(self.delivery.typing)
        self.assertNotIn("secret", json.dumps(self.delivery.snapshot()))
        self.now = 15
        self.transport.notify.side_effect = None
        await self.delivery.step(False)
        self.assertTrue(self.delivery.online_notified)
        self.assertIsNone(self.delivery.lifecycle_error)

    async def test_missing_ticket_is_unknown_and_retries_after_backoff(self):
        self.transport.typing.return_value = False
        await self.delivery.step(True)
        self.assertIsNone(self.delivery.typing)
        self.assertEqual(self.delivery.typing_error, "Unavailable")
        await self.delivery.step(True)
        self.assertEqual(self.transport.typing.call_count, 1)
        self.now = 5
        self.transport.typing.return_value = True
        await self.delivery.step(True)
        self.assertTrue(self.delivery.typing)
        self.assertIsNone(self.delivery.typing_error)

    async def test_shutdown_serializes_inflight_start_then_cancel_then_notify_stop(
        self,
    ):
        calls = []
        entered = threading.Event()
        release = threading.Event()

        def typing(active):
            if active:
                entered.set()
                if not release.wait(2):
                    raise TimeoutError()
            calls.append(("typing", active))
            return True

        self.transport.typing.side_effect = typing
        self.transport.notify.side_effect = lambda active: (
            calls.append(("notify", active)) or True
        )
        stopping = asyncio.Event()
        worker = asyncio.create_task(self.delivery.run(lambda: True, stopping))
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait, 2))
            stopping.set()
            await asyncio.sleep(0)
            self.assertFalse(worker.done())
        finally:
            release.set()
            await asyncio.wait_for(worker, 3)
        self.assertEqual(
            calls,
            [("notify", True), ("typing", True), ("typing", False), ("notify", False)],
        )
        self.assertFalse(self.delivery.online_notified)

    async def test_waiting_disconnect_and_uncertain_sends_do_not_show_typing(self):
        with tempfile.TemporaryDirectory() as directory:
            state = State(Path(directory) / "bridge.sqlite")
            bridge = Bridge(
                {}, state, rpc=Mock(), desktop=Mock(), status_transport=self.transport
            )
            try:
                bridge.connected = True
                bridge.active = {"thread_id": "wechat"}
                self.assertTrue(bridge.typing_needed())
                bridge.requests["q1"] = {"kind": "question"}
                self.assertFalse(bridge.typing_needed())
                bridge.requests.clear()
                bridge.connected = False
                self.assertFalse(bridge.typing_needed())
                bridge.connected = True
                bridge.active = None
                state.enqueue("reply", "结果")
                self.assertTrue(bridge.typing_needed())
                row = state.next_reply()
                for status in ("sending", "blocked", "uncertain", "sent"):
                    state.reply_state(row["id"], status)
                    self.assertEqual(bridge.typing_needed(), status == "sending")
                state.accept("message", "工作", "wechat")
                state.mark("message", "queued")
                self.assertTrue(bridge.typing_needed())
                bridge.stopping.set()
                self.assertFalse(bridge.typing_needed())
            finally:
                state.close()

    async def test_presence_failures_do_not_block_or_duplicate_normal_replies(self):
        with tempfile.TemporaryDirectory() as directory:
            state = State(Path(directory) / "bridge.sqlite")
            sender = Mock(return_value=True)
            self.transport.notify.side_effect = OSError("offline")
            self.transport.typing.side_effect = OSError("offline")
            bridge = Bridge(
                {},
                state,
                rpc=Mock(),
                desktop=Mock(),
                sender=sender,
                status_transport=self.transport,
            )
            bridge.connected = True
            state.enqueue("reply", "结果")
            presence = asyncio.create_task(
                bridge.delivery.run(bridge.typing_needed, bridge.stopping)
            )
            outgoing = asyncio.create_task(bridge.outgoing_worker())
            try:
                for _ in range(100):
                    counts = state.outgoing_counts()
                    if counts["pending"] + counts["sending"] == 0 and sender.called:
                        break
                    await asyncio.sleep(0.01)
                sender.assert_called_once_with({}, "结果")
                self.assertEqual(
                    state.db.execute("SELECT status FROM outbox").fetchone()[0], "sent"
                )
            finally:
                bridge.stopping.set()
                await asyncio.gather(presence, outgoing)
                state.close()

    async def test_bridge_shutdown_notifies_stop_even_if_app_server_close_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            state = State(Path(directory) / "bridge.sqlite")
            rpc = SimpleNamespace(
                start=AsyncMock(),
                close=AsyncMock(side_effect=RuntimeError("close failed")),
            )
            bridge = Bridge(
                {}, state, rpc=rpc, desktop=Mock(), status_transport=self.transport
            )

            async def wait_for_stop(*args):
                await bridge.stopping.wait()

            bridge.model_worker = wait_for_stop
            bridge.outgoing_worker = wait_for_stop
            bridge.monitor = wait_for_stop
            bridge.poll_worker = wait_for_stop
            bridge.desktop.run = wait_for_stop
            worker = asyncio.create_task(bridge.run())
            try:
                for _ in range(100):
                    if bridge.delivery.typing is False:
                        break
                    await asyncio.sleep(0.01)
                self.assertTrue(bridge.delivery.online_notified)
                bridge.stopping.set()
                with self.assertRaisesRegex(RuntimeError, "close failed"):
                    await asyncio.wait_for(worker, 2)
                self.assertEqual(
                    [call.args[0] for call in self.transport.notify.call_args_list],
                    [True, False],
                )
            finally:
                bridge.stopping.set()
                if not worker.done():
                    await worker
                state.close()


if __name__ == "__main__":
    unittest.main()
