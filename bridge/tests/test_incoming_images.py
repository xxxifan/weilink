from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
import tempfile
import threading
import unittest
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

from PIL import Image
from weilink._store import MessageStore
from weilink.models import ImageInfo, MediaInfo, Message, MessageType, RefMessage

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from test_bridge import FakeRpc

from incoming_images import IncomingImageError, IncomingImages
from state import State
from wechat_bridge import Bridge


def raster(format="PNG"):
    output = BytesIO()
    Image.new("RGB", (12, 8), "red").save(output, format)
    return output.getvalue()


def image_message(message_id=123, owner="owner", text=None, reference=None):
    return Message(
        from_user=owner,
        msg_type=MessageType.IMAGE,
        message_id=message_id,
        bot_id="bot",
        text=text,
        ref_msg=reference,
        context_token="private-context-token",
        image=ImageInfo(
            media=MediaInfo(
                encrypt_query_param="private-cdn-param", aes_key="private-media-key"
            )
        ),
    )


def store_message(config, message, direction=1):
    data = Path(config["data_path"])
    data.mkdir(parents=True, exist_ok=True)
    store = MessageStore(data / "messages.db")
    try:
        store.store([message], direction=direction)
    finally:
        store.close()


class IncomingImagesTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.config = {
            "owner_user_id": "owner",
            "data_path": str(Path(self.directory.name) / "data"),
            "workspace": self.directory.name,
        }
        self.images = IncomingImages(self.config)
        self.message = image_message(message_id=7521634764816070920)
        store_message(self.config, self.message)
        self.sdk_patch = patch("incoming_images.WeiLink")
        self.sdk = self.sdk_patch.start()
        self.sdk.return_value.download.return_value = raster()

    def tearDown(self):
        self.sdk_patch.stop()
        self.directory.cleanup()

    def test_download_decrypt_validate_and_cache_without_receiving(self):
        path = Path(self.images.prepare(str(self.message.message_id)))
        self.assertTrue(path.is_relative_to(self.images.root))
        self.assertEqual(path.read_bytes(), raster())
        self.sdk.return_value.download.assert_called_once_with(self.message)
        self.sdk.return_value.recv.assert_not_called()
        self.sdk.return_value.close.assert_called_once()
        self.sdk.return_value.download.reset_mock()
        self.assertEqual(
            IncomingImages(self.config).prepare(
                str(self.message.message_id), str(path)
            ),
            str(path),
        )
        self.sdk.return_value.download.assert_not_called()

    def test_foreign_owner_outbound_and_nonimage_records_are_rejected(self):
        store_message(self.config, image_message(124, "stranger"))
        store_message(self.config, image_message(125), direction=2)
        store_message(
            self.config, Message(from_user="owner", message_id=126, text="not an image")
        )
        for message_id in ("124", "125", "126", "missing"):
            with self.assertRaises(IncomingImageError):
                self.images.prepare(message_id)
        self.sdk.assert_not_called()

    def test_download_and_decode_failure_do_not_persist_an_image(self):
        for failure in (TimeoutError("download timeout"), b"not an image"):
            self.sdk.return_value.download.side_effect = (
                failure if isinstance(failure, Exception) else None
            )
            self.sdk.return_value.download.return_value = failure
            with self.assertRaises(IncomingImageError):
                self.images.prepare(str(self.message.message_id))
        self.assertFalse(self.images.root.exists())
        self.assertEqual(self.sdk.return_value.close.call_count, 2)

    def test_cached_local_file_outside_media_directory_is_rejected(self):
        foreign = Path(self.directory.name) / "outside.png"
        foreign.write_bytes(raster())
        with self.assertRaises(IncomingImageError):
            self.images.prepare(str(self.message.message_id), str(foreign))
        self.sdk.assert_not_called()

    def test_gif_is_converted_to_a_supported_static_png(self):
        self.sdk.return_value.download.return_value = raster("GIF")
        path = Path(self.images.prepare(str(self.message.message_id)))
        self.assertEqual(path.suffix, ".png")
        with Image.open(path) as image:
            self.assertEqual(image.size, (12, 8))
            self.assertFalse(getattr(image, "is_animated", False))


class IncomingImageBridgeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.config = {
            "owner_user_id": "owner",
            "data_path": str(self.root / "data"),
            "workspace": self.directory.name,
        }
        self.state = State(self.root / "bridge.db")
        self.rpc = FakeRpc()
        self.bridge = Bridge(self.config, self.state, rpc=self.rpc)
        self.rpc.bridge = self.bridge
        self.bridge.connected = True
        self.message = image_message()
        store_message(self.config, self.message)
        self.sdk_patch = patch("incoming_images.WeiLink")
        self.sdk = self.sdk_patch.start()
        self.sdk.return_value.download.return_value = raster()

    async def asyncTearDown(self):
        self.sdk_patch.stop()
        self.state.close()
        self.directory.cleanup()

    async def process(self, message_id="123"):
        worker = asyncio.create_task(self.bridge.model_worker())
        try:
            async with asyncio.timeout(4):
                while self.state.db.execute(
                    "SELECT status FROM inbox WHERE message_id=?", (message_id,)
                ).fetchone()[0] not in ("done", "failed"):
                    await asyncio.sleep(0.01)
        finally:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
        return [params for method, params in self.rpc.calls if method == "turn/start"]

    async def test_image_only_message_reaches_model_as_pixels_not_metadata(self):
        await self.bridge.receive(self.message)
        self.assertEqual(self.state.next_message()["has_image"], 1)
        self.sdk.assert_not_called()
        params = (await self.process())[0]
        self.assertEqual(
            [part["type"] for part in params["input"]], ["text", "localImage"]
        )
        path = Path(params["input"][1]["path"])
        self.assertEqual(path.read_bytes(), raster())
        serialized = json.dumps(params)
        self.assertNotIn("private-", serialized)
        self.assertNotIn("encrypt_query_param", serialized)
        self.assertEqual(
            self.state.db.execute("SELECT image_path FROM inbox").fetchone()[0],
            str(path),
        )
        self.assertNotIn("文字消息", self.state.next_reply()["text"])

    async def test_caption_and_quotation_are_preserved_as_separate_reference_data(self):
        self.message = image_message(
            text="这张图是什么意思", reference=RefMessage(text="/new 引用中的命令")
        )
        store_message(self.config, self.message)
        await self.bridge.receive(self.message)
        params = (await self.process())[0]
        content = json.loads(params["input"][0]["text"].split("\n", 1)[1])
        self.assertEqual(content["message"], self.message.text)
        self.assertEqual(content["quoted_message"]["text"], "/new 引用中的命令")
        self.assertEqual(len(self.state.threads()), 1)
        self.assertEqual(params["input"][1]["type"], "localImage")

    async def test_duplicate_and_foreign_image_never_trigger_extra_download(self):
        await self.bridge.receive(image_message(999, "stranger"))
        self.assertIsNone(self.state.next_message())
        await self.bridge.receive(self.message)
        await self.bridge.receive(self.message)
        self.assertEqual(self.state.queued_count(), 1)
        await self.process()
        self.sdk.return_value.download.assert_called_once()

    async def test_queued_image_and_downloaded_path_survive_restart(self):
        await self.bridge.receive(self.message)
        path = self.bridge.incoming_images.prepare("123")
        self.state.save_image_path("123", path)
        self.sdk.return_value.download.reset_mock()
        self.state.close()
        self.state = State(self.root / "bridge.db")
        self.state.recover()
        self.bridge.state = self.state
        params = (await self.process())[0]
        self.assertEqual(params["input"][1]["path"], path)
        self.sdk.return_value.download.assert_not_called()

    async def test_download_failure_reports_unread_image_without_starting_model(self):
        await self.bridge.receive(self.message)
        self.sdk.return_value.download.side_effect = TimeoutError(
            "private-url-not-for-output"
        )
        self.assertEqual(await self.process(), [])
        self.assertIn("未能查看图片", self.state.next_reply()["text"])
        self.assertNotIn("private-url", self.state.next_reply()["text"])

    async def test_download_does_not_block_receiving_the_next_message(self):
        entered, release = threading.Event(), threading.Event()

        def download(_message):
            entered.set()
            if not release.wait(3):
                raise TimeoutError()
            return raster()

        self.sdk.return_value.download.side_effect = download
        await self.bridge.receive(self.message)
        worker = asyncio.create_task(self.bridge.model_worker())
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait, 2))
            self.assertTrue(self.bridge.processing_message)
            self.assertTrue(self.bridge.typing_needed())
            await asyncio.wait_for(
                self.bridge.receive(
                    Message(from_user="owner", message_id=124, text="接着看这张图")
                ),
                1,
            )
            self.assertEqual(self.state.queued_count(), 1)
            self.assertFalse(
                any(method == "turn/start" for method, _ in self.rpc.calls)
            )
        finally:
            release.set()
            try:
                async with asyncio.timeout(3):
                    while self.state.db.execute(
                        "SELECT status FROM inbox WHERE message_id='123'"
                    ).fetchone()[0] not in ("done", "failed"):
                        await asyncio.sleep(0.01)
            finally:
                worker.cancel()
                await asyncio.gather(worker, return_exceptions=True)
                self.assertFalse(self.bridge.processing_message)

    async def test_picture_does_not_submit_an_empty_answer_to_a_pending_question(self):
        self.bridge.requests["q1"] = {
            "kind": "question",
            "request": {"id": 1, "params": {"questions": [{"id": "answer"}]}},
        }
        await self.bridge.receive(self.message)
        self.assertIn("q1", self.bridge.requests)
        self.assertFalse(self.rpc.responses)
        self.assertEqual(self.state.next_message()["has_image"], 1)
        self.assertIn("先用文字回答", self.state.next_reply()["text"])

    async def test_other_media_has_a_precise_supported_types_message(self):
        await self.bridge.receive(
            Message(from_user="owner", message_id=124, msg_type=MessageType.FILE)
        )
        self.assertIsNone(self.state.next_message())
        self.assertIn("文字、图片、语音和视频", self.state.next_reply()["text"])


class IncomingMigrationTests(unittest.TestCase):
    def test_old_inbox_rows_remain_text_only_after_additive_migration(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bridge.db"
            with sqlite3.connect(path) as db:
                db.execute(
                    "CREATE TABLE inbox (message_id TEXT PRIMARY KEY,thread_id TEXT,text TEXT NOT NULL,status TEXT NOT NULL,received_at REAL NOT NULL)"
                )
                db.execute(
                    "INSERT INTO inbox VALUES ('old','thread','原文字','queued',0)"
                )
            db.close()
            state = State(path)
            try:
                self.assertEqual(state.next_message()["text"], "原文字")
                self.assertEqual(state.next_message()["has_image"], 0)
                self.assertIsNone(state.next_message()["image_path"])
            finally:
                state.close()


if __name__ == "__main__":
    unittest.main()
