from __future__ import annotations

import re
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from desktop_relay import reply_prompt
from message_format import WECHAT_RELAY_STYLE, WECHAT_STYLE, copy_text, help_text
from notify import prepare_notification
from state import State
from transport import split_text
from wechat_bridge import Bridge


def blocks(text):
    return re.findall(r"^(`{3,})text\n(.*?)\n\1$", text, flags=re.MULTILINE | re.DOTALL)


class FormatTests(unittest.TestCase):
    def test_copy_block_roundtrips_multiline_commands_including_backticks_and_quotes(
        self,
    ):
        original = 'pwsh -Command "Write-Output `"你好`""\n```\n  保留缩进\n'
        formatted = copy_text(original)
        delimiter, restored = blocks(formatted)[0]
        self.assertEqual(restored, original)
        self.assertGreater(len(delimiter), 3)

    def test_help_operations_are_individually_copyable_and_fit_one_outbound_message(
        self,
    ):
        text = help_text()
        commands = [body for _, body in blocks(text)]
        self.assertIn("/reply t编号 内容", commands)
        self.assertIn("/approve 编号", commands)
        self.assertIn("/deny 编号", commands)
        self.assertIn("/tasks running", commands)
        self.assertTrue(
            all(command.startswith("/") and "\n" not in command for command in commands)
        )
        self.assertLess(len(text.encode("utf8")), 10000)

    def test_notification_preserves_body_and_has_copyable_reply_without_boilerplate(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            state = State(Path(directory) / "state.sqlite")
            try:

                class Monitor:
                    def find(self, reference, excluded):
                        return {"id": reference, "title": "原任务"}

                body = '【任务完成】\n\n修复完成。\n\n```text\npwsh -File "C:/a b/run.ps1"\n```'
                result = prepare_notification(
                    {"owner_user_id": "owner", "bot_id": "bot"},
                    body,
                    "target",
                    state,
                    Monitor(),
                )
                self.assertTrue(result["text"].startswith(body))
                self.assertIn(
                    "/reply t1 消息内容", [b for _, b in blocks(result["text"])]
                )
                self.assertNotIn("任务忙时排队", result["text"])
                self.assertFalse(result["sends_message"])
            finally:
                state.close()

    def test_remote_reply_is_compact_and_preserves_original_request_verbatim(self):
        original = "请检查参数\n```text\n保持内容\n```"
        text = reply_prompt({"id": "sample", "prompt": original})
        self.assertTrue(text.startswith("[微信远程消息 wx-"))
        self.assertIn(WECHAT_RELAY_STYLE, text)
        self.assertNotIn(WECHAT_STYLE, text)
        self.assertNotIn("微信最终答复排版", text)
        self.assertLess(len(text) - len(original), 160)
        self.assertTrue(text.endswith(original))


class SplitFormatTests(unittest.TestCase):
    def assert_preserved(self, original, parts, limit):
        self.assertEqual("".join(parts), original)
        self.assertTrue(all(len(part.encode("utf-8")) <= limit for part in parts))

    def test_paragraphs_are_not_cut_to_fill_previous_message(self):
        first = "第一段说明。\n\n"
        second = "第二段说明。\n\n"
        third = "第三段结束。"
        limit = len((first + second).encode("utf-8")) - 1
        original = first + second + third
        parts = split_text(original, limit)
        self.assertEqual(parts, [first, second + third])
        self.assert_preserved(original, parts, limit)

    def test_copyable_blocks_stay_whole_after_multibyte_prose(self):
        command = 'pwsh -File "C:/a b/run.ps1"\n  保留缩进\n'
        code = copy_text(command)
        prefix = "说明🙂。\n\n" * 4
        original = prefix + code + "\n\n操作完成。"
        limit = len(code.encode("utf-8"))
        parts = split_text(original, limit)
        self.assertIn(code, parts)
        self.assert_preserved(original, parts, limit)

    def test_longer_and_tilde_fences_preserve_embedded_shorter_fences(self):
        for fence in ("````", "~~~"):
            with self.subTest(fence=fence):
                code = f"{fence}text\r\n```\r\n  原文🙂\r\n{fence}\r\n"
                original = "说明\r\n\r\n" * 3 + code + "结束"
                limit = len(code.encode("utf-8"))
                parts = split_text(original, limit)
                self.assertTrue(any(code.rstrip("\r\n") in part for part in parts))
                self.assert_preserved(original, parts, limit)

    def test_oversized_blocks_and_long_lines_preserve_every_character(self):
        for original in (
            "你好🙂" * 100,
            "```text\n" + "  中文🙂\r\n" * 20 + "```",
        ):
            with self.subTest(original=original[:20]):
                parts = split_text(original, 37)
                self.assert_preserved(original, parts, 37)

    def test_empty_text_and_invalid_limits(self):
        self.assertEqual(split_text(""), [""])
        for limit in (0, -1):
            with self.assertRaises(ValueError):
                split_text("text", limit)
        with self.assertRaises(ValueError):
            split_text("🙂", 3)


class ApprovalFormatTests(unittest.IsolatedAsyncioTestCase):
    async def test_copyable_approval_commands_keep_the_real_request_id(self):
        with tempfile.TemporaryDirectory() as directory:
            state = State(Path(directory) / "state.sqlite")
            try:
                bridge = Bridge({"owner_user_id": "owner"}, state, rpc=object())
                bridge.active = {"thread_id": "wechat"}
                await bridge.on_request(
                    {
                        "id": 77,
                        "method": "item/commandExecution/requestApproval",
                        "params": {
                            "threadId": "wechat",
                            "command": 'pwsh -File "C:/a b/run.ps1"',
                        },
                    }
                )
                result = state.next_reply()["text"]
                values = [b for _, b in blocks(result)]
                self.assertIn('pwsh -File "C:/a b/run.ps1"', values)
                self.assertIn("/approve a1", values)
                self.assertIn("/deny a1", values)
                self.assertEqual(bridge.requests["a1"]["request"]["id"], 77)
            finally:
                state.close()


if __name__ == "__main__":
    unittest.main()
