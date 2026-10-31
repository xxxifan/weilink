from __future__ import annotations

import asyncio
import hashlib
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import test_bridge as bridge_tests

from personas import Personas
from state import State
from transport import send_image
from wechat_bridge import CHAT_INSTRUCTIONS, Bridge


class PersonaAssetsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.personas = Personas()

    def test_imported_prompts_and_every_category_are_available(self):
        for key in ("deepseek", "aemeath"):
            text = self.personas.instructions(key, CHAT_INSTRUCTIONS)
            self.assertIn(self.personas.name(key), text)
            self.assertIn("微信桥接适配规则", text)
            self.assertIn("命令和代码继续遵守微信代码块排版", text)
            for category in self.personas.memes[key]:
                self.assertIn(category, self.personas.descriptions[key])
                image = self.personas.select(key, category, "test")
                self.assertTrue(image.is_file())
                self.assertTrue(image.is_relative_to(self.personas.root))
        self.assertEqual(
            self.personas.instructions("off", CHAT_INSTRUCTIONS), CHAT_INSTRUCTIONS
        )

    def test_only_trailing_control_lines_are_converted_to_one_image(self):
        text, image = self.personas.extract(
            "aemeath", "收到收到！\n[[meme:happy]]", "m1"
        )
        self.assertEqual(text, "收到收到！")
        self.assertIn("astrbot-Aemeath-meme-pack", str(image))
        self.assertEqual(image, self.personas.select("aemeath", "happy", "m1"))
        for original in (
            "示例 [[meme:happy]]",
            "```text\n[[meme:happy]]\n```",
            "```text\n[[meme:happy]]",
            "> [[meme:happy]]",
        ):
            self.assertEqual(
                self.personas.extract("aemeath", original, "m1"), (original, None)
            )
        text, image = self.personas.extract("aemeath", "答复\n[[meme:unknown]]", "m1")
        self.assertEqual((text, image), ("答复", None))
        text, image = self.personas.extract("aemeath", "[[meme:unknown]]", "m1")
        self.assertTrue(text)
        self.assertIsNone(image)
        self.assertIsNone(
            self.personas.select("aemeath", "../../bridge-config.json", "m1")
        )

    def test_transport_sends_original_image_bytes_without_receiving(self):
        image = self.personas.select("aemeath", "happy", "transport")
        with patch("transport.QuotedWeiLink") as sdk:
            client = sdk.return_value
            client.send.return_value = True
            self.assertTrue(
                send_image(
                    {"owner_user_id": "owner", "data_path": "unused"}, str(image)
                )
            )
            client.send.assert_called_once_with(
                "owner", image=image.read_bytes(), auto_recv=False
            )
            client.close.assert_called_once()
        with self.assertRaises(ValueError):
            send_image({}, str(Path(__file__).resolve()))

    def test_reviewed_pools_exclude_generic_smile_and_do_not_share_characters(self):
        pools = {}
        for key, expected in (("deepseek", 25), ("aemeath", 41)):
            paths = [p for files in self.personas.memes[key].values() for p in files]
            self.assertTrue(
                all("886dc259f130ae6393bac944c1e4cee0" not in p.name for p in paths)
            )
            pools[key] = {hashlib.sha256(p.read_bytes()).digest() for p in paths}
            self.assertEqual(len(pools[key]), expected)
            self.assertEqual(self.personas.count(key), expected)
        self.assertFalse(pools["deepseek"] & pools["aemeath"])

    def test_animated_gif_is_attached_byte_for_byte_when_file_mode_enabled(self):
        original = self.personas.asset(
            "sources/astrbot-deepseek-meme-pack-7b9d61b09ce068cac621ff2c520ecab11d35d967/memes/happy/1790955203428.gif"
        )
        with Image.open(original) as animation:
            self.assertEqual(animation.n_frames, 207)
        config = {
            "owner_user_id": "owner",
            "data_path": "unused",
            "animated_meme_mode": "file",
        }
        with patch("transport.QuotedWeiLink") as sdk:
            client = sdk.return_value
            client.send.return_value = True
            self.assertTrue(send_image(config, str(original)))
            client.send.assert_called_once_with(
                "owner",
                file=original.read_bytes(),
                file_name=original.name,
                auto_recv=False,
            )
            client.close.assert_called_once()

    def test_file_mode_keeps_static_images_as_images(self):
        original = self.personas.asset(
            "sources/astrbot-Aemeath-meme-pack-36d89a3094044ae55246333f777e868dd154413d/memes/happy/93921A35B5AC6DEDA8D2AF687A922805.jpg"
        )
        config = {
            "owner_user_id": "owner",
            "data_path": "unused",
            "animated_meme_mode": "file",
        }
        with patch("transport.QuotedWeiLink") as sdk:
            client = sdk.return_value
            client.send.return_value = True
            self.assertTrue(send_image(config, str(original)))
            client.send.assert_called_once_with(
                "owner", image=original.read_bytes(), auto_recv=False
            )

    def test_mislabeled_animation_uses_a_gif_attachment_name(self):
        config = {
            "owner_user_id": "owner",
            "data_path": "unused",
            "animated_meme_mode": "file",
        }
        original = self.personas.asset(
            "sources/astrbot-deepseek-meme-pack-7b9d61b09ce068cac621ff2c520ecab11d35d967/memes/happy/1790955203428.gif"
        )
        with (
            patch("transport.QuotedWeiLink") as sdk,
            patch("transport.Path.read_bytes", return_value=original.read_bytes()),
        ):
            client = sdk.return_value
            client.send.return_value = True
            self.assertTrue(send_image(config, str(original.with_suffix(".jpg"))))
            self.assertEqual(client.send.call_args.kwargs["file_name"], original.name)
            self.assertEqual(
                client.send.call_args.kwargs["file"], original.read_bytes()
            )

    def test_short_chat_gets_an_appropriate_character_image_without_marker(self):
        for key in ("deepseek", "aemeath"):
            for message, category in (
                ("你好", "happy"),
                ("你怎么这么可爱", "shy"),
                ("晚安", "sleep"),
                ("心情不好", "sad"),
            ):
                image = self.personas.automatic(key, message, "陪你聊聊", "fallback")
                self.assertIn(image, self.personas.memes[key][category])
        for message, reply in (
            ("你好，帮我修复代码", "修复完成"),
            ("我很难过，家人去世了", "节哀"),
            ("你好", "```text\n保持代码\n```"),
            ("你好", "说明" * 120),
            ("查看状态", "状态正常"),
        ):
            self.assertIsNone(
                self.personas.automatic("deepseek", message, reply, "skip")
            )
        self.assertIsNone(self.personas.automatic("off", "你好", "你好", "skip"))


class PersonaBridgeTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = bridge_tests.BridgeTests.asyncSetUp
    asyncTearDown = bridge_tests.BridgeTests.asyncTearDown
    message = bridge_tests.BridgeTests.message

    async def test_switch_preserves_history_and_refreshes_resume_instructions(self):
        await self.bridge.receive(self.message("/new 原会话"))
        thread = self.state.get("active_thread")
        await self.bridge.receive(self.message("/persona 爱弥斯", "m2"))
        self.assertEqual(self.state.get("active_thread"), thread)
        self.assertEqual(self.bridge.thread_persona(thread), "aemeath")
        self.assertNotIn(thread, self.bridge.resumed)
        await self.bridge.ensure_thread(thread)
        method, params = self.rpc.calls[-1]
        self.assertEqual(method, "thread/resume")
        self.assertIn("当前微信人设：爱弥斯", params["developerInstructions"])
        restarted = Bridge(self.bridge.config, self.state, rpc=self.rpc)
        self.assertEqual(restarted.thread_persona(thread), "aemeath")
        await self.bridge.receive(self.message("/new 第二个会话", "m3"))
        self.assertEqual(
            self.bridge.thread_persona(self.state.get("active_thread")), "off"
        )
        self.assertEqual(self.bridge.thread_persona(thread), "aemeath")

    async def test_inflight_turn_and_unknown_user_cannot_change_persona(self):
        await self.bridge.receive(self.message("/new"))
        thread = self.state.get("active_thread")
        self.bridge.active = {"thread_id": thread}
        await self.bridge.receive(self.message("/persona 大肥鱼", "m2"))
        self.assertEqual(self.bridge.thread_persona(thread), "off")
        self.bridge.active = None
        await self.bridge.receive(
            self.message("/persona 大肥鱼", "m3", user="stranger")
        )
        self.assertEqual(self.bridge.thread_persona(thread), "off")

    async def test_default_and_manual_meme_follow_selected_persona(self):
        self.bridge.config["default_persona"] = "deepseek"
        await self.bridge.receive(self.message("/persona 爱弥斯"))
        thread = self.state.get("active_thread")
        self.assertEqual(self.bridge.thread_persona(thread), "aemeath")
        await self.bridge.receive(self.message("/meme happy", "m2"))
        row = self.state.db.execute(
            "SELECT * FROM outbox WHERE kind='image'"
        ).fetchone()
        self.assertIn("astrbot-Aemeath-meme-pack", row["media_path"])
        await self.bridge.receive(self.message("/persona off", "m3"))
        await self.bridge.ensure_thread(thread)
        self.assertEqual(
            self.rpc.calls[-1][1]["developerInstructions"], CHAT_INSTRUCTIONS
        )

    async def test_model_reply_is_atomically_saved_with_matching_meme(self):
        self.bridge.config["default_persona"] = "aemeath"
        original_event = self.bridge.on_event

        async def with_meme(event):
            if event["method"] == "item/completed":
                event["params"]["item"]["text"] = "收到收到！\n[[meme:happy]]"
            await original_event(event)

        self.bridge.on_event = with_meme
        await self.bridge.receive(self.message("你好"))
        worker = asyncio.create_task(self.bridge.model_worker())
        try:
            for _ in range(100):
                if self.state.next_reply():
                    break
                await asyncio.sleep(0.01)
            rows = self.state.db.execute("SELECT * FROM outbox ORDER BY id").fetchall()
            self.assertEqual([row["kind"] for row in rows], ["text", "image"])
            self.assertEqual(rows[0]["text"], "收到收到！")
            self.assertIn("astrbot-Aemeath-meme-pack", rows[1]["media_path"])
            self.assertIsNone(self.state.next_message())
        finally:
            self.bridge.stopping.set()
            await worker

    async def test_uncertain_image_send_is_not_retried_or_sent_as_text(self):
        calls = []

        def image_send(config, path):
            calls.append(path)
            raise TimeoutError("unknown media delivery outcome")

        self.bridge.image_sender = image_send
        self.bridge.sender = lambda *_: self.fail("Image was routed to text sender")
        path = self.bridge.personas.select("deepseek", "happy", "m1")
        self.state.enqueue_image("m1", str(path))
        worker = asyncio.create_task(self.bridge.outgoing_worker())
        try:
            await asyncio.sleep(0.35)
            self.assertEqual(calls, [str(path)])
            self.assertEqual(
                self.state.db.execute("SELECT status FROM outbox").fetchone()[0],
                "uncertain",
            )
            self.assertIsNone(self.state.next_reply())
        finally:
            self.bridge.stopping.set()
            await worker

    async def test_unmarked_chat_auto_sends_only_selected_character_and_spaces_images(
        self,
    ):
        self.bridge.config["default_persona"] = "deepseek"
        worker = asyncio.create_task(self.bridge.model_worker())

        async def ask(message_id):
            await self.bridge.receive(self.message("你好", message_id))
            for _ in range(150):
                status = self.state.db.execute(
                    "SELECT status FROM inbox WHERE message_id=?", (message_id,)
                ).fetchone()[0]
                if status == "done":
                    break
                await asyncio.sleep(0.01)
            self.assertEqual(status, "done")
            rows = self.state.db.execute(
                "SELECT * FROM outbox WHERE dedupe_key LIKE ? ORDER BY id",
                (f"reply:{message_id}:%",),
            ).fetchall()
            # These are separate chats after delivery, rather than one batch
            # of consecutive messages arriving before the first reply sends.
            for row in rows:
                self.assertTrue(self.state.begin_reply(row["id"]))
                self.state.reply_state(row["id"], "sent")
            return rows

        try:
            first = await ask("auto-1")
            second = await ask("auto-2")
            third = await ask("auto-3")
            self.assertEqual([row["kind"] for row in first], ["text", "image"])
            self.assertEqual([row["kind"] for row in second], ["text"])
            self.assertEqual([row["kind"] for row in third], ["text", "image"])
            self.assertIn(
                Path(first[1]["media_path"]),
                self.bridge.personas.memes["deepseek"]["happy"],
            )
            await self.bridge.receive(self.message("/persona 爱弥斯", "switch-persona"))
            switched = await ask("auto-4")
            self.assertIn(
                Path(switched[1]["media_path"]),
                self.bridge.personas.memes["aemeath"]["happy"],
            )
            self.assertEqual(first[0]["text"], "已处理")
        finally:
            self.bridge.stopping.set()
            await worker


class MediaMigrationTests(unittest.TestCase):
    def test_old_text_outbox_and_new_image_rows_survive_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bridge.sqlite"
            with sqlite3.connect(path) as db:
                db.execute(
                    "CREATE TABLE outbox (id INTEGER PRIMARY KEY AUTOINCREMENT,dedupe_key TEXT UNIQUE NOT NULL,text TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'pending',error TEXT,created_at REAL NOT NULL)"
                )
                db.execute(
                    "INSERT INTO outbox (dedupe_key,text,created_at) VALUES ('old','原通知',0)"
                )
            db.close()
            state = State(path)
            try:
                self.assertEqual(state.next_reply()["kind"], "text")
                state.reply_state(state.next_reply()["id"], "sent")
                state.accept("m1", "请求", "thread")
                state.complete("m1", ["结果"], image_path="installed.gif")
                state.complete("m1", ["结果"], image_path="installed.gif")
                state.reply_state(state.next_reply()["id"], "sent")
                image = state.next_reply()
                self.assertEqual(image["kind"], "image")
                state.reply_state(image["id"], "sending")
            finally:
                state.close()
            state = State(path)
            try:
                state.recover()
                self.assertIsNone(state.next_message())
                self.assertIsNone(state.next_reply())
                self.assertEqual(
                    state.db.execute("SELECT count(*) FROM outbox").fetchone()[0], 3
                )
                self.assertEqual(
                    state.db.execute(
                        "SELECT status FROM outbox WHERE kind='image'"
                    ).fetchone()[0],
                    "uncertain",
                )
            finally:
                state.close()


if __name__ == "__main__":
    unittest.main()
