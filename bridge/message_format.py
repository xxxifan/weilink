"""Phone-readable text cards and copyable command blocks shared by all senders."""

from __future__ import annotations

import re

DIVIDER = "────────"

WECHAT_STYLE = """微信最终答复排版：
• 先说核心结果；短答复自然简洁，不强制套模板。内容多时用【执行结果】【数据明细】【下一步】等短标题分组，组与组可用一行“────────”分隔，不堆叠装饰符或表情。
• 标题、正文段落、每个数据项和操作项之间留一个空行。数据使用“• 名称：数值 单位”；步骤使用编号。保留关键数据、单位和精度，避免宽表格、空格对齐和复杂嵌套列表。
• 让用户复制执行的命令（含 /reply、/task 等 Bot 指令）、配置文本和代码，放进独立的三反引号代码块，默认标记 text。每个独立操作单独一个代码块；说明放在块外，块内只放要复制的内容，不加项目符号、Shell 提示符或解释。代码块必须保留原内容、换行和缩进；真实代码确有需要时可保留语言标记。
• 关键结论可少量加粗。可点击链接直接写完整 https:// 地址，不用 Markdown 文本链接代替。不要把整条说明塞进代码块。
• 不在通知或答复末尾反复追加“任务忙时排队”等机制提醒。用户明确询问机制、当前投递失败或需要处理问题时再说明实际状态。
• 用户指定格式或要求原文时优先遵守；不要为了排版改写命令、代码、日志或数据含义。"""


def copy_text(text: str) -> str:
    # A displayed command/detail can itself contain backticks. Select a
    # longer delimiter so formatting never truncates the copyable content.
    longest = max((len(m.group()) for m in re.finditer(r"`+", text)), default=0)
    fence = "`" * max(3, longest + 1)
    return f"{fence}text\n{text}\n{fence}"


def action(label: str, command: str) -> str:
    return f"{label}\n\n{copy_text(command)}"


def card(title: str, *sections: str) -> str:
    body = f"\n\n{DIVIDER}\n\n".join(part for part in sections if part)
    return f"【{title}】\n\n{body}" if body else f"【{title}】"


def help_text() -> str:
    groups = (
        (
            "微信会话",
            (
                ("开启独立会话", "/new [名称]"),
                ("查看状态与上下文用量", "/status"),
                ("列出微信会话", "/sessions"),
                ("切换微信会话", "/use 编号"),
            ),
        ),
        (
            "人设与表情",
            (
                ("查看当前人设和切换方法", "/persona"),
                ("切换大肥鱼", "/persona 大肥鱼"),
                ("切换爱弥斯", "/persona 爱弥斯"),
                ("关闭角色人设", "/persona off"),
                ("发送当前人设表情（默认 happy）", "/meme [分类]"),
            ),
        ),
        (
            "桌面任务",
            (
                ("列出最近任务或按标题查找", "/tasks [关键词]"),
                ("查看未结束记录", "/tasks running"),
                ("选中任务并查看进展", "/task t编号"),
                ("继续原桌面会话", "/reply t编号 内容"),
                ("查看桌面消息状态", "/replies"),
                ("取消尚未投递的消息", "/cancel r编号"),
                ("刷新选中任务进展", "/progress"),
                ("查看最近 8 条公开消息", "/history [t编号]"),
                ("取消任务查看", "/unwatch"),
            ),
        ),
        (
            "操作控制",
            (
                ("停止当前微信请求", "/stop"),
                ("同意一次操作", "/approve 编号"),
                ("拒绝一次操作", "/deny 编号"),
                ("回答待确认问题", "/answer 编号 内容"),
                ("查看此帮助", "/help"),
            ),
        ),
    )
    sections = [
        f"【{title}】\n\n"
        + "\n\n".join(action(label, command) for label, command in entries)
        for title, entries in groups
    ]
    sections.append(
        "编号使用对应列表或确认消息中的值；方括号表示可选参数。文字、图片、语音和视频进入当前微信会话。视频支持 3 分钟、100 MB 以内，按时间抽样画面并尝试识别音轨。"
    )
    return card("微信 Codex · 使用帮助", *sections)
