from __future__ import annotations

import asyncio
import base64
import json
import sys
import tempfile
import threading
import time
import unittest
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from generated_images import GeneratedImages
from state import State
from transport import send_image
from wechat_bridge import Bridge


def raster(color="red"):
    buffer = BytesIO()
    Image.new("RGB", (8, 8), color).save(buffer, "PNG")
    return base64.b64encode(buffer.getvalue()).decode("ascii")


def append(path, kind, payload):
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({"type": kind, "payload": payload}) + "\n")


class ImageRpc:
    def __init__(self, path):
        self.path = path
        self.path.write_text("", encoding="utf-8")
        self.bridge = None
        self.images = [raster()]
        self.public_events = False
        self.final_text = "图片画好了"
        self.number = 0

    async def request(self, method, params):
        if method in ("thread/start", "thread/resume"):
            return {"thread": {"id": "wechat-images", "path": str(self.path)}}
        if method != "turn/start":
            return {}
        self.number += 1
        turn = f"turn-{self.number}"
        append(self.path, "event_msg", {"type": "task_started", "turn_id": turn})
        append(self.path, "turn_context", {"turn_id": turn})
        items = []
        for index, result in enumerate(self.images):
            raw = {
                "type": "image_generation_call",
                "status": "completed",
                "result": result,
            }
            append(self.path, "response_item", raw)
            if self.public_events:
                item = {**raw, "type": "imageGeneration", "id": f"image-{index}"}
                await self.bridge.on_event(
                    {
                        "method": "item/completed",
                        "params": {
                            "threadId": params["threadId"],
                            "turnId": turn,
                            "item": item,
                        },
                    }
                )
                items.append(item)
        if self.final_text:
            items.append(
                {
                    "type": "agentMessage",
                    "id": "answer",
                    "phase": "final_answer",
                    "text": self.final_text,
                }
            )
        append(self.path, "event_msg", {"type": "task_complete", "turn_id": turn})
        await self.bridge.on_event(
            {
                "method": "turn/completed",
                "params": {
                    "threadId": params["threadId"],
                    "turn": {"id": turn, "status": "completed", "items": items},
                },
            }
        )
        return {"turn": {"id": turn}}


class GeneratedImageTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.images = GeneratedImages(self.root / "data" / "generated-images")

    def tearDown(self):
        self.directory.cleanup()

    def test_scoped_rollout_recovers_only_current_turn_and_deduplicates(self):
        path = self.root / "rollout.jsonl"
        append(path, "turn_context", {"turn_id": "old"})
        append(
            path,
            "response_item",
            {
                "type": "image_generation_call",
                "status": "completed",
                "result": raster("blue"),
            },
        )
        offset = self.images.checkpoint(str(path))
        append(path, "turn_context", {"turn_id": "current"})
        item = {
            "type": "image_generation_call",
            "status": "completed",
            "result": raster(),
        }
        append(path, "response_item", item)
        append(path, "event_msg", {"type": "task_complete", "turn_id": "current"})
        append(path, "turn_context", {"turn_id": "other"})
        append(path, "response_item", {**item, "result": raster("green")})
        paths, failed = self.images.collect([item], str(path), "current", offset)
        self.assertFalse(failed)
        self.assertEqual(len(paths), 1)
        self.assertEqual(Path(paths[0]).read_bytes(), base64.b64decode(item["result"]))
        self.assertEqual(len(list(self.images.root.iterdir())), 1)

    def test_completed_event_racing_a_partial_rollout_flush_keeps_image(self):
        path = self.root / "rollout.jsonl"
        append(path, "turn_context", {"turn_id": "current"})
        item = {
            "type": "image_generation_call",
            "status": "completed",
            "result": raster(),
        }
        encoded = json.dumps({"type": "response_item", "payload": item})
        with path.open("a", encoding="utf-8") as stream:
            stream.write(encoded[:50])

        def finish():
            time.sleep(0.1)
            with path.open("a", encoding="utf-8") as stream:
                stream.write(encoded[50:] + "\n")
            append(path, "event_msg", {"type": "task_complete", "turn_id": "current"})

        writer = threading.Thread(target=finish)
        writer.start()
        try:
            paths, failed = self.images.collect([], str(path), "current", 0)
            self.assertFalse(failed)
            self.assertEqual(len(paths), 1)
        finally:
            writer.join()

    def test_invalid_failed_and_path_only_results_never_send_local_files(self):
        for item in (
            {"result": "not base64"},
            {"result": base64.b64encode(b"not an image").decode()},
            {"result": raster(), "status": "failed"},
            {"savedPath": str(self.root / "secret.png")},
            {"fileId": "unresolved-image"},
        ):
            paths, failed = self.images.collect(
                [{"type": "imageGeneration", "status": "completed", **item}],
                None,
                "turn",
                0,
            )
            self.assertEqual(paths, [])
            self.assertTrue(failed)

    def test_data_url_and_atomic_persistence_preserve_original_bytes(self):
        data = raster()
        path = self.images.save({"result": "data:image/png;base64," + data})
        self.assertEqual(path.read_bytes(), base64.b64decode(data))
        self.assertEqual(GeneratedImages(self.images.root).save({"result": data}), path)
        self.assertEqual(list(self.images.root.iterdir()), [path])

    def test_transport_allows_generated_raster_but_rejects_other_local_files(self):
        path = self.images.save({"result": raster()})
        with (
            patch("transport.ROOT", self.root),
            patch("transport.QuotedWeiLink") as sdk,
        ):
            sdk.return_value.send.return_value = True
            self.assertTrue(
                send_image({"owner_user_id": "owner", "data_path": "unused"}, str(path))
            )
            sdk.return_value.send.assert_called_once_with(
                "owner", image=path.read_bytes(), auto_recv=False
            )
            with self.assertRaises(ValueError):
                send_image({}, str(self.root / "secret.png"))


class ImageDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.state = State(self.root / "bridge.db")
        self.rpc = ImageRpc(self.root / "rollout.jsonl")
        self.bridge = Bridge(
            {"owner_user_id": "owner", "workspace": str(self.root)},
            self.state,
            rpc=self.rpc,
            generated_images=GeneratedImages(self.root / "data" / "generated-images"),
        )
        self.rpc.bridge = self.bridge
        self.bridge.connected = True

    async def asyncTearDown(self):
        self.state.close()
        self.directory.cleanup()

    async def ask(self, message_id="m1"):
        await self.bridge.receive(
            SimpleNamespace(
                text="给我画张头像", message_id=message_id, from_user="owner"
            )
        )
        worker = asyncio.create_task(self.bridge.model_worker())
        try:
            async with asyncio.timeout(3):
                while self.state.db.execute(
                    "SELECT status FROM inbox WHERE message_id=?", (message_id,)
                ).fetchone()[0] not in ("done", "failed"):
                    await asyncio.sleep(0.01)
        finally:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
        return [
            dict(row)
            for row in self.state.db.execute(
                "SELECT * FROM outbox WHERE dedupe_key LIKE ? ORDER BY id",
                (f"reply:{message_id}:%",),
            )
        ]

    async def send_pending(self):
        worker = asyncio.create_task(self.bridge.outgoing_worker())
        try:
            async with asyncio.timeout(3):
                while (
                    self.state.outgoing_counts()["pending"]
                    or self.state.outgoing_counts()["sending"]
                ):
                    await asyncio.sleep(0.01)
        finally:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)

    async def test_original_missing_public_event_delivers_images_before_text(self):
        self.rpc.images.append(raster("blue"))
        rows = await self.ask()
        self.assertEqual([row["kind"] for row in rows], ["image", "image", "text"])
        calls = []
        self.bridge.image_sender = lambda _config, path: (
            calls.append(("image", Path(path).read_bytes())) or True
        )
        self.bridge.sender = lambda _config, text: calls.append(("text", text)) or True
        await self.send_pending()
        self.assertEqual([call[0] for call in calls], ["image", "image", "text"])
        self.assertEqual(calls[0][1], base64.b64decode(self.rpc.images[0]))
        self.assertTrue(
            all(
                row[0] == "sent"
                for row in self.state.db.execute("SELECT status FROM outbox")
            )
        )

    async def test_event_rollout_and_turn_copies_do_not_duplicate_images(self):
        self.rpc.public_events = True
        rows = await self.ask()
        self.assertEqual([row["kind"] for row in rows], ["image", "text"])

    async def test_image_only_reply_does_not_claim_no_content(self):
        self.rpc.final_text = ""
        rows = await self.ask()
        self.assertEqual([row["kind"] for row in rows], ["image"])

    async def test_later_text_turn_does_not_resend_historical_images(self):
        await self.ask()
        self.bridge.image_sender = lambda *_: True
        self.bridge.sender = lambda *_: True
        await self.send_pending()
        self.rpc.images = []
        self.rpc.final_text = "你好"
        rows = await self.ask("m2")
        self.assertEqual([row["kind"] for row in rows], ["text"])

    async def test_invalid_image_replaces_unverified_success_prose(self):
        self.rpc.images = ["broken data"]
        rows = await self.ask()
        self.assertEqual([row["kind"] for row in rows], ["text"])
        self.assertIn("没有可发送的图片", rows[0]["text"])
        self.assertNotIn("画好了", rows[0]["text"])

    async def test_multiple_timeouts_suppress_success_and_report_failure_once(self):
        self.rpc.images.append(raster("blue"))
        await self.ask()
        image_calls, texts = [], []

        def timeout(_config, path):
            image_calls.append(path)
            raise TimeoutError("unknown upload outcome")

        self.bridge.image_sender = timeout
        self.bridge.sender = lambda _config, text: texts.append(text) or True
        await self.send_pending()
        self.assertEqual(len(image_calls), 2)
        self.assertEqual(len(texts), 1)
        self.assertIn("无法确认", texts[0])
        self.assertNotIn("画好了", texts[0])
        self.state.recover()
        self.assertIsNone(self.state.next_reply())

    async def test_blocked_upload_can_retry_after_new_message_without_success_claim(
        self,
    ):
        await self.ask()
        self.bridge.image_sender = lambda *_: False
        texts = []
        self.bridge.sender = lambda _config, text: texts.append(text) or True
        await self.send_pending()
        self.assertEqual(len(texts), 1)
        self.assertIn("发送被阻止", texts[0])
        self.assertEqual(self.state.outgoing_counts()["blocked"], 1)
        self.state.unblock()
        self.assertEqual(self.state.next_reply()["kind"], "image")

    async def test_crash_keeps_raster_and_replaces_pending_success_text(self):
        rows = await self.ask()
        self.state.reply_state(rows[0]["id"], "sending")
        self.state.close()
        self.state = State(self.root / "bridge.db")
        self.state.recover()
        self.assertTrue(Path(rows[0]["media_path"]).is_file())
        self.assertIn("无法确认", self.state.next_reply()["text"])
        self.assertEqual(self.state.outgoing_counts()["uncertain"], 1)

    async def test_stale_image_event_cannot_enter_next_turn(self):
        self.bridge.active = {
            "thread_id": "wechat-images",
            "turn_id": "new",
            "items": {},
        }
        await self.bridge.on_event(
            {
                "method": "item/completed",
                "params": {
                    "threadId": "wechat-images",
                    "turnId": "old",
                    "item": {
                        "type": "imageGeneration",
                        "id": "stale",
                        "status": "completed",
                        "result": raster(),
                    },
                },
            }
        )
        self.assertEqual(self.bridge.active["items"], {})


if __name__ == "__main__":
    unittest.main()
