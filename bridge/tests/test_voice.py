from __future__ import annotations

import asyncio
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from weilink._store import MessageStore
from weilink.models import MediaInfo, Message, MessageType, VoiceInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import transport
from state import State
from voice import (
    VoiceError,
    Voices,
    explicit_text_reply,
    reply_mode,
    speech_command,
    voice_attachment,
    voice_metadata,
)
from wechat_bridge import Bridge


def voice_fixture(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    path = root / "sample.silk"
    data = b"\x02#!SILK_V3\x00fixture"
    path.write_bytes(data)
    path.with_suffix(".json").write_text(
        json.dumps(
            {
                "sha256": hashlib.sha256(data).hexdigest(),
                "encode_type": 6,
                "sample_rate": 24000,
                "bits_per_sample": 16,
                "playtime": 2000,
            }
        ),
        encoding="utf-8",
    )
    return path


def incoming_message(owner="owner", message_id=123):
    return Message(
        from_user=owner,
        message_id=message_id,
        msg_type=MessageType.VOICE,
        bot_id="bot",
        context_token="private-context",
        voice=VoiceInfo(
            media=MediaInfo(encrypt_query_param="private-cdn", aes_key="private-key")
        ),
    )


class SpeechRuntimeTests(unittest.TestCase):
    def test_default_paths_are_resolved_from_installation_and_user_home(self):
        with patch.dict(
            "voice.os.environ",
            {
                "WEILINK_VOICE_RUNTIME": "",
                "WEILINK_VOICE_SCRIPT": "",
                "CODEX_HOME": "",
            },
        ):
            command = speech_command()
        runtime = Path(__file__).resolve().parents[3] / "tts"
        self.assertEqual(Path(command[0]), runtime / ".venv" / "Scripts" / "python.exe")
        self.assertEqual(
            Path(command[3]),
            Path.home() / ".codex" / "skills" / "edge-voice" / "scripts" / "speech.py",
        )

    def test_environment_overrides_support_an_independent_speech_installation(self):
        with patch.dict(
            "voice.os.environ",
            {
                "WEILINK_VOICE_RUNTIME": "C:/portable-tts",
                "WEILINK_VOICE_SCRIPT": "C:/portable-skills/speech.py",
            },
        ):
            command = speech_command()
        self.assertEqual(
            Path(command[0]), Path("C:/portable-tts/.venv/Scripts/python.exe")
        )
        self.assertEqual(Path(command[3]), Path("C:/portable-skills/speech.py"))

    def test_codex_home_override_locates_the_installed_skill(self):
        with patch.dict(
            "voice.os.environ",
            {
                "WEILINK_VOICE_SCRIPT": "",
                "CODEX_HOME": "C:/portable-codex",
            },
        ):
            self.assertEqual(
                Path(speech_command()[3]),
                Path("C:/portable-codex/skills/edge-voice/scripts/speech.py"),
            )


class VoiceInputTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.config = {
            "owner_user_id": "owner",
            "workspace": str(self.root),
            "data_path": str(self.root / "data"),
        }
        Path(self.config["data_path"]).mkdir()
        self.voices = Voices(self.config, self.root / "outgoing")

    def tearDown(self):
        self.directory.cleanup()

    def store(self, message, direction=1):
        store = MessageStore(Path(self.config["data_path"]) / "messages.db")
        try:
            store.store([message], direction=direction)
        finally:
            store.close()

    def test_owner_audio_is_downloaded_once_without_claiming_the_receiver(self):
        message = incoming_message()
        self.store(message)
        with (
            patch("voice.WeiLink") as sdk,
            patch(
                "voice.run_speech", return_value={"text": "请告诉我今天的安排"}
            ) as recognition,
        ):
            sdk.return_value.download.return_value = b"\x02#!SILK_V3audio"
            path, text = self.voices.incoming("123")
            self.assertEqual(text, "请告诉我今天的安排")
            self.assertTrue(Path(path).is_relative_to(self.voices.incoming_root))
            sdk.return_value.download.assert_called_once_with(message)
            sdk.return_value.recv.assert_not_called()
            sdk.return_value.close.assert_called_once()
            recognition.assert_called_once_with("--transcribe", path)

    def test_other_owner_outgoing_records_and_non_voice_cannot_be_transcribed(self):
        for message, direction in (
            (incoming_message(owner="other"), 1),
            (incoming_message(), 2),
            (
                Message(
                    from_user="owner",
                    message_id=123,
                    msg_type=MessageType.TEXT,
                    text="secret",
                ),
                1,
            ),
        ):
            with self.subTest(message=message, direction=direction):
                self.store(message, direction)
                with patch("voice.WeiLink") as sdk, self.assertRaises(VoiceError):
                    self.voices.incoming("123")
                sdk.assert_not_called()

    def test_code_and_links_remain_copyable_alongside_spoken_text(self):
        self.voices.root.mkdir(parents=True)
        path = self.voices.root / "reply.mp3"
        path.write_bytes(b"ID3fixture")
        with patch(
            "voice.run_speech", return_value={"paths": [str(path)]}
        ) as generation:
            paths, keep = self.voices.outgoing(
                "运行下面命令：\n```text\n/status\n```\n详情 https://example.com"
            )
            self.assertTrue(keep)
            self.assertEqual(paths, [str(path)])
            spoken = generation.call_args.args[1]
            self.assertNotIn("/status", spoken)
            self.assertNotIn("https://", spoken)
            self.assertNotIn("--wechat", generation.call_args.args)


class VoiceTransportTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.path = voice_fixture(self.root / "data" / "generated-voices")

    def tearDown(self):
        self.directory.cleanup()

    def test_native_voice_supplies_codec_duration_and_does_not_leak_to_other_sends(
        self,
    ):
        sent = []
        with (
            patch("transport.ROOT", self.root),
            patch("transport.QuotedWeiLink") as sdk,
            patch("transport._post", return_value={"ret": 0}) as post,
        ):

            def send(*args, **kwargs):
                body = {
                    "msg": {"item_list": [{"type": 3, "voice_item": {"media": {}}}]}
                }
                transport.checked_post(
                    transport.proto.EP_SEND_MESSAGE, body, "private", "unused"
                )
                sent.append(body)
                return True

            sdk.return_value.send.side_effect = send
            self.assertTrue(
                transport.send_voice(
                    {"owner_user_id": "owner", "data_path": "unused"}, str(self.path)
                )
            )
            sdk.return_value.send.assert_called_once_with(
                "owner", voice=self.path.read_bytes(), auto_recv=False
            )
            self.assertEqual(
                sent[0]["msg"]["item_list"][0]["voice_item"]["encode_type"], 6
            )
            self.assertEqual(
                sent[0]["msg"]["item_list"][0]["voice_item"]["playtime"], 2000
            )
            plain = {"msg": {"item_list": [{"type": 3, "voice_item": {"media": {}}}]}}
            transport.checked_post(
                transport.proto.EP_SEND_MESSAGE, plain, "private", "unused"
            )
            self.assertNotIn("playtime", plain["msg"]["item_list"][0]["voice_item"])
            self.assertEqual(post.call_count, 2)

    def test_external_files_and_modified_payloads_are_not_sent(self):
        with self.assertRaises(ValueError):
            voice_metadata(self.path, self.root / "unrelated")
        self.path.write_bytes(b"\x02#!SILK_V3modified")
        with self.assertRaises(ValueError):
            voice_metadata(self.path, self.path.parent)

    def test_mp3_uses_file_delivery_instead_of_silent_native_voice(self):
        path = self.path.with_suffix(".mp3")
        path.write_bytes(b"ID3fixture")
        with (
            patch("transport.ROOT", self.root),
            patch("transport.QuotedWeiLink") as sdk,
        ):
            sdk.return_value.send.return_value = True
            self.assertTrue(
                transport.send_voice(
                    {"owner_user_id": "owner", "data_path": "unused"}, str(path)
                )
            )
            sdk.return_value.send.assert_called_once_with(
                "owner", file=b"ID3fixture", file_name="晓伊语音.mp3", auto_recv=False
            )
            sdk.return_value.close.assert_called_once()

    def test_attachment_rejects_external_empty_and_non_mp3_files(self):
        path = self.path.with_suffix(".mp3")
        path.write_bytes(b"ID3fixture")
        with self.assertRaises(ValueError):
            voice_attachment(path, self.root / "other")
        for data in (b"", b"not audio"):
            path.write_bytes(data)
            with self.subTest(data=data), self.assertRaises(ValueError):
                voice_attachment(path, path.parent)


class VoiceRpc:
    def __init__(self):
        self.bridge = None
        self.final_text = "你好，已经理解你的意思了。"
        self.inputs = []
        self.answers = []

    async def request(self, method, params):
        if method in ("thread/start", "thread/resume"):
            return {"thread": {"id": "voice-thread"}}
        if method == "turn/start":
            self.inputs.append(params["input"])
            await self.bridge.on_event(
                {
                    "method": "item/completed",
                    "params": {
                        "threadId": "voice-thread",
                        "turnId": "voice-turn",
                        "item": {
                            "id": "reply",
                            "type": "agentMessage",
                            "phase": "final_answer",
                            "text": self.final_text,
                        },
                    },
                }
            )
            await self.bridge.on_event(
                {
                    "method": "turn/completed",
                    "params": {
                        "threadId": "voice-thread",
                        "turn": {"id": "voice-turn", "status": "completed"},
                    },
                }
            )
            return {"turn": {"id": "voice-turn"}}
        return {}

    async def respond(self, request_id, result):
        self.answers.append((request_id, result))


class VoiceBridgeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.state = State(self.root / "bridge.db")
        self.rpc = VoiceRpc()
        self.voices = Mock()
        self.voices.incoming.return_value = (
            str(self.root / "received.audio"),
            "你好，给我讲个笑话",
        )
        self.voices.outgoing.return_value = ([str(self.root / "reply.silk")], False)
        self.bridge = Bridge(
            {"owner_user_id": "owner", "workspace": str(self.root)},
            self.state,
            rpc=self.rpc,
            voices=self.voices,
        )
        self.bridge.connected = True
        self.rpc.bridge = self.bridge

    async def asyncTearDown(self):
        self.state.close()
        self.directory.cleanup()

    async def ask(self, voice=True, message_id=123):
        message = (
            incoming_message(message_id=message_id)
            if voice
            else Message(
                from_user="owner",
                message_id=message_id,
                msg_type=MessageType.TEXT,
                text="你好",
            )
        )
        await self.bridge.receive(message)
        worker = asyncio.create_task(self.bridge.model_worker())
        try:
            async with asyncio.timeout(3):
                while self.state.db.execute(
                    "SELECT status FROM inbox WHERE message_id=?", (str(message_id),)
                ).fetchone()[0] not in ("done", "failed"):
                    await asyncio.sleep(0.01)
        finally:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
        return [
            dict(row)
            for row in self.state.db.execute("SELECT * FROM outbox ORDER BY id")
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

    async def test_voice_is_understood_and_defaults_to_voice_without_duplicate_text(
        self,
    ):
        rows = await self.ask()
        self.assertEqual([row["kind"] for row in rows], ["voice"])
        self.assertIn("讲个笑话", self.rpc.inputs[0][0]["text"])
        self.voices.outgoing.assert_called_once_with(self.rpc.final_text)
        self.bridge.voice_sender = Mock(return_value=True)
        self.bridge.sender = Mock(return_value=True)
        await self.send_pending()
        self.bridge.voice_sender.assert_called_once()
        self.bridge.sender.assert_not_called()

    async def test_explicit_text_response_overrides_voice_default(self):
        self.voices.incoming.return_value = (
            "received.audio",
            "用文字回复，告诉我你的名字",
        )
        self.rpc.final_text = "[[reply:text]]\n我是助手。"
        rows = await self.ask()
        self.assertEqual([row["kind"] for row in rows], ["text"])
        self.assertEqual(rows[0]["text"], "我是助手。")
        self.voices.outgoing.assert_not_called()

    async def test_audio_attachment_is_explained_without_losing_copyable_text(self):
        self.voices.outgoing.return_value = ([str(self.root / "reply.mp3")], False)
        rows = await self.ask()
        self.assertEqual([row["kind"] for row in rows], ["voice", "text"])
        self.assertIn("MP3", rows[1]["text"])
        self.assertNotIn(self.rpc.final_text, rows[1]["text"])
        self.rpc.final_text = "运行命令：\n```text\n/status\n```"
        self.voices.outgoing.return_value = ([str(self.root / "reply.mp3")], True)
        rows = await self.ask(message_id=124)
        self.assertIn("/status", rows[-1]["text"])

    async def test_text_defaults_to_text_but_can_request_voice(self):
        rows = await self.ask(voice=False)
        self.assertEqual([row["kind"] for row in rows], ["text"])
        self.voices.incoming.assert_not_called()
        self.rpc.final_text = "[[reply:voice]]\n这次用语音回答。"
        rows = await self.ask(voice=False, message_id=124)
        self.assertEqual([row["kind"] for row in rows], ["text", "voice"])
        self.voices.outgoing.assert_called_once_with("这次用语音回答。")

    async def test_direct_text_request_is_honored_even_if_model_forgets_marker(self):
        self.voices.incoming.return_value = (
            "received.audio",
            "请用文字回复，告诉我你的名字",
        )
        rows = await self.ask()
        self.assertEqual([row["kind"] for row in rows], ["text"])
        self.voices.outgoing.assert_not_called()

    async def test_asr_failure_does_not_invent_a_user_request(self):
        self.voices.incoming.side_effect = VoiceError("unrecognized")
        rows = await self.ask()
        self.assertIn("未能识别", rows[0]["text"])
        self.assertFalse(self.rpc.inputs)

    async def test_tts_failure_returns_the_actual_answer_as_text(self):
        self.voices.outgoing.side_effect = VoiceError("offline")
        rows = await self.ask()
        self.assertEqual([row["kind"] for row in rows], ["text"])
        self.assertIn("语音生成失败", rows[0]["text"])
        self.assertIn(self.rpc.final_text, rows[0]["text"])

    async def test_spoken_slash_command_cannot_approve_an_operation(self):
        self.voices.incoming.return_value = ("received.audio", "/approve a1")
        self.bridge.requests["a1"] = {"kind": "approval"}
        await self.bridge.receive(incoming_message())
        self.assertIn("a1", self.bridge.requests)
        self.assertEqual(self.state.next_message()["transcription"], "/approve a1")

    async def test_voice_answers_a_pending_question_without_losing_default_voice(self):
        self.bridge.active = {"voice_reply": False}
        self.bridge.requests["q1"] = {
            "kind": "question",
            "request": {"id": 1, "params": {"questions": [{"id": "choice"}]}},
        }
        await self.bridge.receive(incoming_message())
        self.assertNotIn("q1", self.bridge.requests)
        self.assertTrue(self.bridge.active["voice_reply"])
        self.assertIn(
            "讲个笑话", self.rpc.answers[0][1]["answers"]["choice"]["answers"][0]
        )

    async def test_uncertain_voice_send_is_not_replayed_after_recovery(self):
        self.voices.outgoing.return_value = (["reply.silk"], True)
        await self.ask()
        self.bridge.voice_sender = Mock(side_effect=TimeoutError("unknown outcome"))
        texts = []
        self.bridge.sender = lambda _config, text: texts.append(text) or True
        await self.send_pending()
        self.assertEqual(self.bridge.voice_sender.call_count, 1)
        self.assertEqual(len(texts), 1)
        self.assertIn("语音已生成", texts[0])
        self.assertIn("无法确认", texts[0])
        self.state.recover()
        self.assertIsNone(self.state.next_reply())

    async def test_transcription_survives_restart_and_is_not_repeated(self):
        await self.bridge.receive(incoming_message())
        self.state.recover()
        self.assertEqual(
            self.state.next_message()["transcription"], "你好，给我讲个笑话"
        )
        self.voices.incoming.assert_called_once()


class ReplyModeTests(unittest.TestCase):
    def test_negated_text_and_quoted_examples_do_not_force_text_reply(self):
        self.assertTrue(explicit_text_reply("请用文字回复，告诉我结果"))
        self.assertTrue(explicit_text_reply("以后不要发语音"))
        self.assertFalse(explicit_text_reply("不要用文字回复，用语音"))
        self.assertFalse(explicit_text_reply("解释一下‘用文字回复’是什么意思"))

    def test_only_explicit_reply_markers_change_the_default(self):
        self.assertEqual(reply_mode("你好", True), ("你好", True))
        self.assertEqual(reply_mode("[[reply:text]]\n你好", True), ("你好", False))
        self.assertEqual(reply_mode("[[reply:voice]]\n你好", False), ("你好", True))


if __name__ == "__main__":
    unittest.main()
