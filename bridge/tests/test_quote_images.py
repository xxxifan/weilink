from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import time
import unittest
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image
from weilink._store import MessageStore
from weilink.models import (
    BotInfo,
    ImageInfo,
    MediaInfo,
    Message,
    MessageType,
    RefMessage,
    UploadedMedia,
    UploadMediaType,
)

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from test_bridge import FakeRpc

from incoming_images import IncomingImageError
from quote_images import QuotedImages, QuoteImageCache
from quoted_messages import QuotedWeiLink, message_reference
from state import State
from transport import send_image
from wechat_bridge import Bridge


def raster():
    output = BytesIO()
    Image.new("RGB", (12, 8), "red").save(output, "PNG")
    return output.getvalue()


def image_info():
    return ImageInfo(
        media=MediaInfo(encrypt_query_param="private-cdn", aes_key="private-key")
    )


class QuoteImageTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.data = self.root / "data"
        self.data.mkdir()
        self.config = {
            "owner_user_id": "owner",
            "data_path": str(self.data),
            "workspace": str(self.root),
        }
        self.images = QuotedImages(self.config)

    def tearDown(self):
        self.directory.cleanup()

    def store(self, message):
        store = MessageStore(self.data / "messages.db")
        try:
            store.store([message])
        finally:
            store.close()

    def test_inline_quoted_image_downloads_pixels_without_receiving_or_exposing_keys(
        self,
    ):
        self.store(
            Message(
                from_user="owner",
                bot_id="bot",
                message_id=10,
                text="引用图",
                ref_msg=RefMessage(msg_type=MessageType.IMAGE, image=image_info()),
            )
        )
        with patch("quote_images.WeiLink") as sdk:
            sdk.return_value.download.return_value = raster()
            path = self.images.prepare("10", {"type": "IMAGE"})
            self.assertEqual(Path(path).read_bytes(), raster())
            supplied = sdk.return_value.download.call_args.args[0]
            self.assertEqual(supplied.image, image_info())
            self.assertEqual(supplied.msg_type, MessageType.IMAGE)
            sdk.return_value.recv.assert_not_called()
            sdk.return_value.close.assert_called_once()

    def test_id_only_reference_downloads_original_owner_image_and_reuses_cache(self):
        self.store(
            Message(
                from_user="owner",
                bot_id="bot",
                message_id=11,
                msg_type=MessageType.IMAGE,
                image=image_info(),
            )
        )
        reference = {
            "type": "IMAGE",
            "message_id": "11",
            "source_message_id": "11",
            "sender": "user",
        }
        with patch("quote_images.WeiLink") as sdk:
            sdk.return_value.download.return_value = raster()
            path = self.images.prepare("99", reference)
            self.assertEqual(Path(path).read_bytes(), raster())
            self.assertEqual(sdk.return_value.download.call_count, 1)
            fresh = QuotedWeiLink(base_path=self.data, message_store=True)
            try:
                cached = fresh.quote_store.get("bot", "owner", "11")
            finally:
                fresh.close()
            self.assertEqual(QuotedImages(self.config).prepare("99", cached), path)
            self.assertEqual(sdk.return_value.download.call_count, 1)

    def test_foreign_owner_and_arbitrary_local_file_cannot_be_read_as_quoted_image(
        self,
    ):
        self.store(
            Message(
                from_user="stranger",
                bot_id="bot",
                message_id=12,
                msg_type=MessageType.IMAGE,
                image=image_info(),
            )
        )
        self.store(
            Message(from_user="owner", bot_id="bot", message_id=13, text="no media")
        )
        outside = self.root / "private.png"
        outside.write_bytes(raster())
        with patch("quote_images.WeiLink") as sdk:
            for reference in ({"message_id": "12"}, {"image_path": str(outside)}):
                with self.assertRaises(IncomingImageError):
                    self.images.prepare("13", reference)
            sdk.assert_not_called()

    def prepare_sender(self):
        client = QuotedWeiLink(base_path=self.data, message_store=True)
        session = client._default_session
        session.bot_info = BotInfo(
            bot_id="bot", base_url="https://test.invalid", token="fake-token"
        )
        session.context_tokens["owner"] = "fake-context"
        session.context_timestamps["owner"] = time.time()
        client._save_session_state(session)
        client._save_session_contexts(session)
        client.close()

    def send_raster(self, path, server_id):
        uploaded = UploadedMedia(
            media_type=UploadMediaType.IMAGE,
            filekey="fake",
            download_param="private-cdn",
            aes_key_hex="0" * 32,
            file_size=100,
            cipher_size=112,
        )
        with (
            patch("transport.ROOT", self.root),
            patch("weilink._cdn.upload_media", return_value=uploaded),
            patch(
                "transport._post", return_value={"ret": 0, "message_id": server_id}
            ) as api,
        ):
            self.assertTrue(send_image(self.config, str(path)))
            return api.call_args.args[1]["msg"]["item_list"][0]["type"]

    def test_real_media_send_caches_server_id_original_pixels_and_sender_after_restart(
        self,
    ):
        self.prepare_sender()
        source = self.data / "generated-images" / "sent.png"
        source.parent.mkdir()
        source.write_bytes(raster())
        server_id = str(2**64 - 9)
        self.assertEqual(self.send_raster(source, server_id), 2)
        source.unlink()
        client = QuotedWeiLink(base_path=self.data, message_store=True)
        try:
            message = client._parse_message(
                {
                    "from_user_id": "owner",
                    "message_id": 20,
                    "item_list": [
                        {
                            "type": 1,
                            "text_item": {"text": "这张谁发的"},
                            "ref_msg": {
                                "message_item": {"type": 2, "msg_id": server_id}
                            },
                        }
                    ],
                },
                bot_id="bot",
            )
            reference = message_reference(message)
            self.assertEqual(reference["type"], "IMAGE")
            self.assertEqual(reference["sender"], "bot")
            self.assertEqual(reference["message_id"], server_id)
            self.assertEqual(
                Path(self.images.prepare("20", reference)).read_bytes(), raster()
            )
            self.assertNotIn("private-", json.dumps(reference))
        finally:
            client.close()

    def test_animated_image_sent_as_file_has_a_viewable_static_quote(self):
        self.prepare_sender()
        source = self.data / "generated-images" / "sent.gif"
        source.parent.mkdir()
        Image.new("RGB", (12, 8), "red").save(
            source,
            "GIF",
            save_all=True,
            append_images=[Image.new("RGB", (12, 8), "blue")],
            duration=100,
        )
        self.config["animated_meme_mode"] = "file"
        self.assertEqual(self.send_raster(source, "1002"), 4)
        client = QuotedWeiLink(base_path=self.data, message_store=True)
        try:
            reference = client.quote_store.get("bot", "owner", "1002")
            self.assertEqual(reference["type"], "IMAGE")
            with Image.open(reference["image_path"]) as frame:
                self.assertEqual(frame.format, "PNG")
                self.assertEqual(frame.convert("RGB").getpixel((0, 0)), (255, 0, 0))
        finally:
            client.close()

    def test_caption_and_image_sent_together_keep_their_own_server_ids_and_types(self):
        self.prepare_sender()
        client = QuotedWeiLink(base_path=self.data, message_store=True)
        uploaded = UploadedMedia(
            media_type=UploadMediaType.IMAGE,
            filekey="fake",
            download_param="private-cdn",
            aes_key_hex="0" * 32,
            file_size=100,
            cipher_size=112,
        )
        try:
            with (
                patch("weilink._cdn.upload_media", return_value=uploaded),
                patch(
                    "transport._post",
                    side_effect=[
                        {"ret": 0, "message_id": "2001"},
                        {"ret": 0, "message_id": "2002"},
                    ],
                ),
            ):
                self.assertTrue(
                    client.send("owner", "图片说明", image=raster(), auto_recv=False)
                )
            caption = client.quote_store.get("bot", "owner", "2001")
            picture = client.quote_store.get("bot", "owner", "2002")
            self.assertEqual(caption["type"], "TEXT")
            self.assertEqual(caption["text"], "图片说明")
            self.assertNotIn("image_path", caption)
            self.assertEqual(picture["type"], "IMAGE")
            self.assertEqual(Path(picture["image_path"]).read_bytes(), raster())
            inline = client._parse_message(
                {
                    "from_user_id": "owner",
                    "message_id": 20,
                    "item_list": [
                        {
                            "type": 1,
                            "text_item": {"text": "谁发的"},
                            "ref_msg": {
                                "svr_id": "2002",
                                "message_item": {
                                    "type": 2,
                                    "image_item": {
                                        "media": {
                                            "encrypt_query_param": "private-cdn",
                                            "aes_key": "private-key",
                                        }
                                    },
                                },
                            },
                        }
                    ],
                },
                bot_id="bot",
            )
            self.assertEqual(message_reference(inline)["sender"], "bot")
            self.assertEqual(
                message_reference(inline)["image_path"], picture["image_path"]
            )
        finally:
            client.close()

    def test_bad_or_missing_quoted_image_does_not_persist_unreadable_bytes(self):
        with self.assertRaises((OSError, ValueError)):
            QuoteImageCache(self.data).save(b"not an image")
        self.store(
            Message(
                from_user="owner",
                bot_id="bot",
                message_id=30,
                text="missing",
                ref_msg=RefMessage(msg_type=MessageType.IMAGE, image=image_info()),
            )
        )
        with patch("quote_images.WeiLink") as sdk:
            sdk.return_value.download.side_effect = TimeoutError("private url")
            with self.assertRaises(IncomingImageError):
                self.images.prepare("30", {})
            sdk.return_value.close.assert_called_once()


class QuotedImageBridgeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.config = {
            "owner_user_id": "owner",
            "data_path": str(self.root / "data"),
            "workspace": str(self.root),
        }
        self.state = State(self.root / "bridge.db")
        self.rpc = FakeRpc()
        self.bridge = Bridge(self.config, self.state, rpc=self.rpc)
        self.rpc.bridge = self.bridge
        self.bridge.connected = True
        self.path = QuoteImageCache(self.root / "data").save(raster())

    async def asyncTearDown(self):
        self.state.close()
        self.directory.cleanup()

    def message(self, path=None):
        return SimpleNamespace(
            from_user="owner",
            message_id="99",
            text="这个是谁发的，画了什么",
            quoted_context={
                "type": "IMAGE",
                "sender": "bot",
                "message_id": "123",
                "image_path": path or self.path,
            },
        )

    async def process(self):
        worker = asyncio.create_task(self.bridge.model_worker())
        try:
            async with asyncio.timeout(4):
                while self.state.db.execute(
                    "SELECT status FROM inbox WHERE message_id='99'"
                ).fetchone()[0] not in ("done", "failed"):
                    await asyncio.sleep(0.01)
        finally:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
        return next(
            params for method, params in self.rpc.calls if method == "turn/start"
        )

    async def test_quoted_pixels_id_and_known_sender_reach_model_and_survive_queue_restart(
        self,
    ):
        await self.bridge.receive(self.message())
        self.state.close()
        self.state = State(self.root / "bridge.db")
        self.state.recover()
        self.bridge.state = self.state
        params = await self.process()
        pictures = [item for item in params["input"] if item["type"] == "localImage"]
        self.assertEqual(len(pictures), 1)
        self.assertEqual(Path(pictures[0]["path"]).read_bytes(), raster())
        content = json.loads(params["input"][0]["text"].split("\n", 1)[1])
        self.assertTrue(content["quoted_message"]["image_attached"])
        self.assertEqual(content["quoted_message"]["sender"], "bot")
        self.assertEqual(content["quoted_message"]["message_id"], "123")
        self.assertTrue(self.state.message_reference("99")["image_attached"])

    async def test_missing_quote_image_keeps_body_and_id_without_claiming_image_was_read(
        self,
    ):
        await self.bridge.receive(self.message(str(self.root / "missing.png")))
        params = await self.process()
        self.assertFalse(any(item["type"] == "localImage" for item in params["input"]))
        content = json.loads(params["input"][0]["text"].split("\n", 1)[1])
        self.assertFalse(content["quoted_message"]["image_attached"])
        self.assertEqual(content["message"], "这个是谁发的，画了什么")
        self.assertEqual(content["quoted_message"]["message_id"], "123")

    async def test_current_image_and_quoted_image_are_both_sent_with_separate_context(
        self,
    ):
        message = self.message()
        message.msg_type = MessageType.IMAGE
        message.image = image_info()
        await self.bridge.receive(message)
        own_path = self.root / ".wechat-images" / "current.png"
        own_path.parent.mkdir()
        own_path.write_bytes(raster())
        with patch.object(
            self.bridge.incoming_images, "prepare", return_value=str(own_path)
        ):
            params = await self.process()
        paths = [
            item["path"] for item in params["input"] if item["type"] == "localImage"
        ]
        self.assertEqual(paths, [str(own_path), self.path])
        captions = [item["text"] for item in params["input"] if item["type"] == "text"]
        self.assertTrue(any("引用的图片" in caption for caption in captions))

    async def test_quoted_picture_waits_for_real_image_turn_instead_of_becoming_text_answer(
        self,
    ):
        self.bridge.requests["q1"] = {
            "kind": "question",
            "request": {"id": 78, "params": {"questions": [{"id": "choice"}]}},
        }
        await self.bridge.receive(self.message())
        self.assertFalse(self.rpc.responses)
        self.assertEqual(self.state.queued_count(), 1)


if __name__ == "__main__":
    unittest.main()
