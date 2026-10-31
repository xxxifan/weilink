from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

BRIDGE = Path(__file__).resolve().parents[1]
BOOTSTRAP = """
import sys
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, sys.argv[1])
import notify

class Monitor:
    def find(self, reference, excluded):
        return {"id": reference, "title": "\\u539f\\u4efb\\u52a1"}

text_file = Path(sys.argv[2])
sys.argv = ["notify", "--prepare", "--text-file", str(text_file), "--thread-id", "source-thread"]
with (
    patch("notify.ROOT", text_file.parent),
    patch("notify.load_config", return_value={"owner_user_id": "owner", "bot_id": "bot"}),
    patch("notify.TaskMonitor", return_value=Monitor()),
    patch("notify.send_text", side_effect=AssertionError("prepare must never send")),
):
    notify.main()
"""


class NotificationEncodingTests(unittest.TestCase):
    def prepare(self, text: str, stdout_encoding: str) -> bytes:
        with tempfile.TemporaryDirectory() as directory:
            text_file = Path(directory) / "notification.txt"
            text_file.write_text(text, encoding="utf-8")
            result = subprocess.run(
                [sys.executable, "-c", BOOTSTRAP, str(BRIDGE), str(text_file)],
                env={
                    **os.environ,
                    "PYTHONUTF8": "0",
                    "PYTHONIOENCODING": stdout_encoding,
                },
                capture_output=True,
                timeout=15,
                check=True,
            )
            self.assertEqual(text_file.read_text(encoding="utf-8"), text)
            return result.stdout

    def assert_round_trip(self, text: str, encoding: str) -> None:
        raw = self.prepare(text, encoding)
        # Tool readers interpret stdout as UTF-8 even on a GBK desktop. JSON
        # must survive that exact boundary, not merely a matching GBK decoder.
        decoded = raw.decode("utf-8")
        self.assertTrue(raw.isascii())
        prepared = json.loads(decoded)
        self.assertTrue(prepared["text"].startswith(text + "\n\n"))
        self.assertIn("原任务", prepared["text"])
        self.assertIn("/reply t1 消息内容", prepared["text"])
        self.assertNotIn("\ufffd", prepared["text"])
        self.assertEqual(prepared["to"], "owner")
        self.assertFalse(prepared["sends_message"])

    def test_chinese_notification_survives_gbk_stdout_and_utf8_reader(self):
        self.assert_round_trip("【公开仓库审计】当前版本已清理，历史清理待确认", "gbk")

    def test_emoji_and_supplementary_characters_survive_legacy_stdout(self):
        self.assert_round_trip("语音和图片 🐟：𠮷，保留换行\n以及中文", "gbk")

    def test_ascii_and_utf8_stdout_preserve_the_same_notification(self):
        for encoding in ("ascii", "utf-8"):
            with self.subTest(encoding=encoding):
                self.assert_round_trip("完成通知：修复已验证 ✅", encoding)


if __name__ == "__main__":
    unittest.main()
