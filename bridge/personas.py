"""Per-conversation character voices and local, category-selected meme packs."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

ASSETS = Path(__file__).resolve().parent.parent / "personas"
ALIASES = {
    "大肥鱼": "deepseek",
    "鲸鱼": "deepseek",
    "爱弥斯": "aemeath",
    "小爱": "aemeath",
    "关闭": "off",
    "默认": "off",
}
MEME_MARKER = re.compile(r"(?:^|\n)\[\[meme:([a-z_]+)\]\]\s*$")
# A missed model marker should not make ordinary character chat text-only.
# Keep fallback matching narrow; serious/work replies still depend on an
# intentional model selection, and fallback images are spaced by the bridge.
CHAT_MOODS = (
    ("sleep", r"晚安|睡觉|困了|睡了|休息了"),
    ("morning", r"早上好|早安|起床"),
    ("sad", r"难过|伤心|失落|委屈|心情不好|呜呜"),
    ("baka", r"傻鱼|笨蛋|你真笨|你就知道吃|吃吃吃|大胆|别闹|抓狂"),
    ("shy", r"可爱|真好看|夸夸|害羞|喜欢你|爱你"),
    ("surprised", r"真的假的|居然|竟然|震惊"),
    ("happy", r"哈哈|嘿嘿|笑死|绷不住|来啦|来了|你好|哈喽|ciallo|吃饭|大白饭|开心"),
)
WORK_CONTEXT = re.compile(
    r"代码|脚本|修复|排查|配置|接口|日志|报错|构建|提交|部署|诊断|就医|急救|药物|自杀|自残|去世|病危",
    re.IGNORECASE,
)


class Personas:
    def __init__(self, root: Path = ASSETS) -> None:
        self.root = root.resolve()
        catalog = self.root / "catalog.json"
        self.entries = json.loads(catalog.read_text(encoding="utf-8"))
        # The upstream packs contain unrelated characters and generic emoji.
        # Only visually reviewed files belong to a persona's sending pool;
        # keep the unmodified downloads outside that pool for reference.
        approved = json.loads(
            (self.root / "approved-assets.json").read_text(encoding="utf-8")
        )
        self.memes: dict[str, dict[str, list[Path]]] = {}
        self.descriptions: dict[str, dict[str, str]] = {}
        for key, entry in self.entries.items():
            pack = self.asset(entry["pack"])
            manifest = json.loads(
                (pack / "manifest.json").read_text(encoding="utf-8-sig")
            )
            # Upstream category names include invisible Unicode characters;
            # normalize them to match the actual on-disk category directories.
            self.descriptions[key] = {
                re.sub(r"[^a-z_]", "", category): info["description"]
                for category, info in manifest["categories"].items()
            }
            categories = {}
            for directory in sorted((pack / "memes").iterdir()):
                if not directory.is_dir():
                    continue
                files = [
                    p.resolve()
                    for p in sorted(directory.iterdir())
                    if p.is_file()
                    and p.relative_to(self.root).as_posix() in approved.get(key, [])
                    and p.suffix.lower() in (".png", ".jpg", ".jpeg", ".gif", ".webp")
                    and p.resolve().is_relative_to(self.root)
                ]
                if files:
                    categories[directory.name] = files
            self.memes[key] = categories

    def asset(self, relative: str) -> Path:
        path = (self.root / relative).resolve()
        if not path.is_relative_to(self.root):
            raise ValueError("人设资源路径超出本地目录")
        return path

    def resolve(self, argument: str) -> str:
        key = ALIASES.get(argument.strip(), argument.strip().lower())
        if key not in self.entries and key != "off":
            raise ValueError("可选人设：大肥鱼、爱弥斯、off（关闭）。")
        return key

    def name(self, key: str) -> str:
        return self.entries[key]["name"] if key != "off" else "默认助手"

    def count(self, key: str) -> int:
        return len(
            {
                hashlib.sha256(path.read_bytes()).digest()
                for paths in self.memes.get(key, {}).values()
                for path in paths
            }
        )

    def instructions(self, key: str, base: str) -> str:
        if key == "off":
            return base
        entry = self.entries[key]
        prompt = self.asset(entry["prompt"]).read_text(encoding="utf-8")
        categories = "\n".join(
            f"{category}：{self.descriptions[key].get(category, category)}"
            for category in self.memes[key]
        )
        return (
            base
            + f"\n\n当前微信人设：{entry['name']}。以下是用户选择的角色语气参考：\n"
            + prompt
            + "\n\n微信桥接适配规则（优先于上述角色参考中的冲突内容）：\n"
            "角色设定只用于语气和虚构扮演，不改变真实模型身份、能力、事实、工具权限和用户授权边界。"
            "被直接问到真实身份时如实回答；不把角色经历当作真实经历，不编造已执行的操作。"
            "日常闲聊按人设简短自然回复；代码、排障、数据、工作任务仍完整处理并清楚交付，"
            "命令和代码继续遵守微信代码块排版，不因人设省略重要结果或确认。"
            "原文中的 ** 等游戏偏好占位符不直接输出，也不擅自补成用户偏好。\n"
            "日常闲聊时主动按语境配当前人设表情，不要等用户索要，也不要让用户必须输入 /meme。"
            "问候、玩笑、夸奖、撒娇、开心、困惑和晚安等互动中自然使用，通常每一到两轮配一张即可。"
            "只能使用当前人设的图片，不能拿普通 emoji、猫狗或其他角色代替。"
            "需要配图时在最终答复末尾单独一行写 [[meme:分类]]，"
            "例如 [[meme:happy]]。桥接器会移除该行并发送本地图片。每次最多一张，"
            "不为配图调用微信发送工具，不输出图片路径，不在代码块内写控制标记。"
            "正式或严肃内容可不配表情。可用分类（仅描述使用场景，不代表定时任务或操作授权）：\n"
            + categories
        )

    def select(self, key: str, category: str, seed: str) -> Path | None:
        files = self.memes.get(key, {}).get(category, [])
        if not files:
            return None
        index = int.from_bytes(hashlib.sha256(seed.encode("utf-8")).digest()[:8])
        return files[index % len(files)]

    def automatic(self, key: str, user_text: str, reply: str, seed: str) -> Path | None:
        if key == "off" or len(user_text) > 160 or len(reply) > 220:
            return None
        if "```" in user_text + reply or WORK_CONTEXT.search(user_text + reply):
            return None
        if "[[meme:" in reply:
            return None
        for category, pattern in CHAT_MOODS:
            if re.search(pattern, user_text, re.IGNORECASE):
                return self.select(key, category, seed)
        return None

    def extract(self, key: str, text: str, seed: str) -> tuple[str, Path | None]:
        match = MEME_MARKER.search(text)
        if key == "off" or match is None:
            return text, None
        # Only consume a standalone trailing control line outside code fences.
        # Code examples and quoted markers must survive byte-for-byte.
        prefix = text[: match.start()]
        fence = None
        for line in prefix.splitlines():
            opening = re.match(r"^\s*(`{3,}|~{3,})", line)
            if opening:
                delimiter = opening[1]
                if fence is None:
                    fence = delimiter
                elif delimiter[0] == fence[0] and len(delimiter) >= len(fence):
                    fence = None
        if fence is not None:
            return text, None
        image = self.select(key, match[1], seed)
        body = prefix.rstrip()
        if not body and image is None:
            body = "这次处理已结束，没有返回文字内容。"
        return body, image
