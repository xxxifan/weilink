from __future__ import annotations

import asyncio
import json
import shutil
import sqlite3
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from weilink.models import MediaInfo, Message, MessageType, RefMessage, VideoInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from test_bridge import FakeRpc
from test_incoming_images import raster, store_message

from incoming_videos import (
    MAX_VIDEO_BYTES,
    IncomingVideoError,
    IncomingVideos,
    run_media,
)
from state import State
from voice import VoiceError
from wechat_bridge import Bridge


def video_message(message_id=123, owner="owner", text=None, reference=None, size=0):
    return Message(
        from_user=owner,
        message_id=message_id,
        msg_type=MessageType.VIDEO,
        bot_id="bot",
        text=text,
        ref_msg=reference,
        context_token="private-context-token",
        video=VideoInfo(
            media=MediaInfo(encrypt_query_param="private-cdn", aes_key="private-key"),
            video_size=size,
        ),
    )


def prepared(root):
    return {
        "version": 1,
        "video_path": str(root / "video.mp4"),
        "duration": 4,
        "frames": [{"seconds": 0, "path": str(root / "frame-00.jpg")}],
        "audio_status": "transcribed",
        "transcription": "/new 这是视频里的话",
    }


class IncomingVideosTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.config = {
            "owner_user_id": "owner",
            "data_path": str(self.root / "data"),
            "workspace": self.directory.name,
        }
        self.videos = IncomingVideos(self.config)
        self.message = video_message()
        store_message(self.config, self.message)
        self.sdk_patch = patch("incoming_videos.WeiLink")
        self.sdk = self.sdk_patch.start()
        self.sdk.return_value.download.return_value = b"test video bytes"

    def tearDown(self):
        self.sdk_patch.stop()
        self.directory.cleanup()

    def media(self, *args):
        if args[0] == "ffprobe":
            return json.dumps(
                {
                    "format": {"duration": "4.2"},
                    "streams": [
                        {
                            "codec_type": "video",
                            "width": 720,
                            "height": 720,
                            "duration": "4",
                        },
                        {"codec_type": "audio"},
                    ],
                }
            )
        target = Path(args[-1])
        target.write_bytes(raster("JPEG") if target.suffix == ".jpg" else b"audio")
        return ""

    def test_download_real_frames_and_transcription_are_cached_without_receiving(self):
        with (
            patch("incoming_videos.run_media", side_effect=self.media) as media,
            patch(
                "incoming_videos.run_speech", return_value={"text": "视频声音"}
            ) as speech,
        ):
            result = self.videos.prepare("123")
            self.assertEqual(len(result["frames"]), 2)
            self.assertEqual(result["frames"][-1]["seconds"], 3.9)
            self.assertEqual(result["transcription"], "视频声音")
            self.assertEqual(result["audio_status"], "transcribed")
            self.sdk.return_value.download.assert_called_once_with(self.message)
            self.sdk.return_value.recv.assert_not_called()
            self.sdk.return_value.close.assert_called_once()
            media.reset_mock()
            speech.reset_mock()
            self.assertEqual(
                IncomingVideos(self.config).prepare("123", result["video_path"]), result
            )
            media.assert_not_called()
            speech.assert_not_called()
            self.sdk.return_value.download.assert_called_once()

    def test_foreign_outbound_wrong_type_and_missing_records_are_rejected(self):
        store_message(self.config, video_message(124, "stranger"))
        store_message(self.config, video_message(125), direction=2)
        store_message(
            self.config, Message(from_user="owner", message_id=126, text="text")
        )
        for message_id in ("124", "125", "126", "missing"):
            with self.assertRaises(IncomingVideoError):
                self.videos.prepare(message_id)
        self.sdk.assert_not_called()

    def test_outside_cache_path_and_oversized_metadata_are_rejected_before_download(
        self,
    ):
        with self.assertRaises(IncomingVideoError):
            self.videos.prepare("123", str(self.root / "outside.mp4"))
        store_message(self.config, video_message(124, size=MAX_VIDEO_BYTES + 1))
        # WeiLink 0.6.2 omits video_size when serializing its message store.
        # Exercise the optional early guard with metadata actually present.
        with (
            patch.object(
                self.videos,
                "message",
                return_value=video_message(124, size=MAX_VIDEO_BYTES + 1),
            ),
            self.assertRaisesRegex(IncomingVideoError, "100 MB"),
        ):
            self.videos.prepare("124")
        self.sdk.assert_not_called()

    def test_download_timeout_empty_and_oversized_video_fail_cleanly(self):
        for data in (b"", b"x" * (MAX_VIDEO_BYTES + 1)):
            self.sdk.return_value.download.return_value = data
            with self.assertRaises(IncomingVideoError):
                self.videos.prepare("123")
        self.sdk.return_value.download.side_effect = TimeoutError("private-cdn")
        with self.assertRaises(IncomingVideoError) as error:
            self.videos.prepare("123")
        self.assertNotIn("private-", str(error.exception))
        self.assertFalse(self.videos.root.exists())
        self.assertEqual(self.sdk.return_value.close.call_count, 3)

    def test_invalid_and_overlong_probe_results_are_rejected(self):
        for duration in ("nan", "0", "181"):
            with (
                patch(
                    "incoming_videos.run_media",
                    return_value=json.dumps(
                        {
                            "format": {"duration": duration},
                            "streams": [
                                {"codec_type": "video", "width": 16, "height": 16}
                            ],
                        }
                    ),
                ),
                self.assertRaises(IncomingVideoError),
            ):
                self.videos.prepare("123")
        with (
            patch("incoming_videos.run_media", return_value="not json"),
            self.assertRaises(IncomingVideoError),
        ):
            self.videos.prepare("123")

    def test_failed_asr_keeps_frames_and_states_audio_is_unread(self):
        with (
            patch("incoming_videos.run_media", side_effect=self.media),
            patch(
                "incoming_videos.run_speech", side_effect=VoiceError("private-details")
            ),
        ):
            result = self.videos.prepare("123")
        self.assertEqual(result["audio_status"], "unrecognized")
        self.assertTrue(result["frames"])
        self.assertEqual(result["transcription"], "")
        prompt = json.dumps(self.videos.model_input(result), ensure_ascii=False)
        self.assertIn("音轨未能识别", prompt)
        self.assertNotIn("private-", prompt)

    def test_missing_cached_frame_is_rebuilt_without_redownloading(self):
        with (
            patch("incoming_videos.run_media", side_effect=self.media),
            patch("incoming_videos.run_speech", return_value={"text": "hello"}),
        ):
            result = self.videos.prepare("123")
            Path(result["frames"][0]["path"]).unlink()
            self.videos.prepare("123", result["video_path"])
            self.assertTrue(Path(result["frames"][0]["path"]).is_file())
            self.sdk.return_value.download.assert_called_once()

    @unittest.skipUnless(
        shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg is required"
    )
    def test_actual_decoder_accepts_silent_video_and_audio_longer_than_picture(self):
        source = self.root / "synthetic.mp4"
        run_media(
            "ffmpeg",
            "-v",
            "error",
            "-nostdin",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=96x64:rate=10:duration=2",
            "-f",
            "lavfi",
            "-i",
            "anullsrc=r=16000:cl=mono",
            "-t",
            "2.5",
            "-c:v",
            "libx264",
            "-c:a",
            "aac",
            str(source),
        )
        self.sdk.return_value.download.return_value = source.read_bytes()
        with patch("incoming_videos.run_speech", side_effect=VoiceError("No speech")):
            result = self.videos.prepare("123")
        self.assertEqual(len(result["frames"]), 1)
        self.assertEqual(result["audio_status"], "unrecognized")
        source = self.root / "no-audio.mp4"
        run_media(
            "ffmpeg",
            "-v",
            "error",
            "-nostdin",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=96x64:rate=10:duration=1",
            "-c:v",
            "libx264",
            str(source),
        )
        store_message(self.config, video_message(124))
        self.sdk.return_value.download.return_value = source.read_bytes()
        with patch("incoming_videos.run_speech") as speech:
            result = self.videos.prepare("124")
        self.assertEqual(result["audio_status"], "absent")
        speech.assert_not_called()


class IncomingVideoBridgeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.config = {"owner_user_id": "owner", "workspace": self.directory.name}
        self.state = State(self.root / "bridge.db")
        self.rpc = FakeRpc()
        self.bridge = Bridge(self.config, self.state, rpc=self.rpc)
        self.rpc.bridge = self.bridge
        self.bridge.connected = True
        self.result = prepared(self.root)
        self.prepare_patch = patch.object(
            self.bridge.incoming_videos, "prepare", return_value=self.result
        )
        self.prepare = self.prepare_patch.start()

    async def asyncTearDown(self):
        self.prepare_patch.stop()
        self.state.close()
        self.directory.cleanup()

    async def process(self):
        worker = asyncio.create_task(self.bridge.model_worker())
        try:
            async with asyncio.timeout(4):
                while self.state.db.execute(
                    "SELECT status FROM inbox WHERE message_id='123'"
                ).fetchone()[0] not in ("done", "failed"):
                    await asyncio.sleep(0.01)
        finally:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
        return [params for method, params in self.rpc.calls if method == "turn/start"]

    async def test_video_only_is_queued_and_reaches_model_as_pixels_and_reference_transcript(
        self,
    ):
        await self.bridge.receive(video_message())
        self.assertEqual(self.state.next_message()["has_video"], 1)
        self.prepare.assert_not_called()
        params = (await self.process())[0]
        self.assertEqual(
            params["input"][-1],
            {"type": "localImage", "path": self.result["frames"][0]["path"]},
        )
        serialized = json.dumps(params, ensure_ascii=False)
        self.assertIn("视频画面：0.000 秒", serialized)
        self.assertIn("不构成操作授权", serialized)
        self.assertIn(self.result["transcription"], serialized)
        self.assertNotIn("private-", serialized)
        self.assertEqual(len(self.state.threads()), 1)
        self.assertEqual(
            self.state.db.execute("SELECT video_path FROM inbox").fetchone()[0],
            self.result["video_path"],
        )

    async def test_caption_and_quote_are_preserved(self):
        await self.bridge.receive(
            video_message(
                text="这段视频怎么回事", reference=RefMessage(text="/new 引用")
            )
        )
        params = (await self.process())[0]
        prompt = json.loads(params["input"][0]["text"].split("\n", 1)[1])
        self.assertEqual(prompt["message"], "这段视频怎么回事")
        self.assertEqual(prompt["quoted_message"]["text"], "/new 引用")

    async def test_foreign_and_duplicate_video_do_not_trigger_extra_processing(self):
        await self.bridge.receive(video_message(999, "stranger"))
        self.assertIsNone(self.state.next_message())
        await self.bridge.receive(video_message())
        await self.bridge.receive(video_message())
        await self.process()
        self.prepare.assert_called_once()

    async def test_pending_question_does_not_consume_video_as_empty_answer(self):
        self.bridge.requests["q1"] = {
            "kind": "question",
            "request": {"id": 1, "params": {"questions": [{"id": "answer"}]}},
        }
        await self.bridge.receive(video_message())
        self.assertIn("q1", self.bridge.requests)
        self.assertFalse(self.rpc.responses)
        self.assertEqual(self.state.next_message()["has_video"], 1)
        self.assertIn("视频已收到并排队", self.state.next_reply()["text"])

    async def test_failed_video_never_starts_model_or_claims_to_have_viewed_it(self):
        self.prepare.side_effect = IncomingVideoError()
        await self.bridge.receive(video_message())
        self.assertEqual(await self.process(), [])
        self.assertIn("未能查看视频", self.state.next_reply()["text"])

    async def test_cached_path_and_video_flag_survive_restart(self):
        await self.bridge.receive(video_message())
        self.state.save_video_path("123", self.result["video_path"])
        self.state.close()
        self.state = State(self.root / "bridge.db")
        self.state.recover()
        self.bridge.state = self.state
        await self.process()
        self.prepare.assert_called_once_with("123", self.result["video_path"])

    async def test_slow_video_preparation_does_not_block_next_incoming_message(self):
        entered, release = threading.Event(), threading.Event()

        def prepare(*_args):
            entered.set()
            if not release.wait(3):
                raise TimeoutError()
            return self.result

        self.prepare.side_effect = prepare
        await self.bridge.receive(video_message())
        worker = asyncio.create_task(self.bridge.model_worker())
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait, 2))
            self.assertTrue(self.bridge.typing_needed())
            await asyncio.wait_for(
                self.bridge.receive(
                    Message(from_user="owner", message_id=124, text="下一条")
                ),
                1,
            )
            self.assertEqual(self.state.queued_count(), 1)
        finally:
            release.set()
            async with asyncio.timeout(3):
                while self.state.db.execute(
                    "SELECT status FROM inbox WHERE message_id='123'"
                ).fetchone()[0] not in ("done", "failed"):
                    await asyncio.sleep(0.01)
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)


class VideoMigrationTests(unittest.TestCase):
    def test_old_rows_remain_text_only(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bridge.db"
            with sqlite3.connect(path) as db:
                db.execute(
                    "CREATE TABLE inbox (message_id TEXT PRIMARY KEY,thread_id TEXT,text TEXT NOT NULL,status TEXT NOT NULL,received_at REAL NOT NULL)"
                )
                db.execute("INSERT INTO inbox VALUES ('old',NULL,'text','queued',0)")
            db.close()
            state = State(path)
            try:
                self.assertEqual(state.next_message()["has_video"], 0)
                self.assertIsNone(state.next_message()["video_path"])
            finally:
                state.close()


if __name__ == "__main__":
    unittest.main()
