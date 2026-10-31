"""Owner-only voice input and durable Xiaoyi audio replies."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import subprocess
import uuid
from contextlib import closing
from pathlib import Path

from weilink import WeiLink
from weilink._store import deserialize_message
from weilink.models import MessageType

REPLY_MARKER = re.compile(r"(?m)^\s*\[\[reply:(voice|text)\]\]\s*$")
MAX_VOICE_BYTES = 20 * 1024 * 1024


class VoiceError(RuntimeError):
    pass


def speech_command() -> list[str]:
    # Resolve local installations at runtime. Published source must not carry
    # a developer's home directory; overrides also support other layouts.
    runtime = Path(
        os.environ.get("WEILINK_VOICE_RUNTIME")
        or Path(__file__).resolve().parents[2] / "tts"
    ).expanduser()
    codex_home = Path(
        os.environ.get("CODEX_HOME") or Path.home() / ".codex"
    ).expanduser()
    script = Path(
        os.environ.get("WEILINK_VOICE_SCRIPT")
        or codex_home / "skills" / "edge-voice" / "scripts" / "speech.py"
    ).expanduser()
    return [
        str(runtime / ".venv" / "Scripts" / "python.exe"),
        "-X",
        "utf8",
        str(script),
    ]


def run_speech(*arguments: str) -> dict:
    try:
        result = subprocess.run(
            [*speech_command(), *arguments],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=True,
            timeout=240,
        )
        return json.loads(result.stdout)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        # Do not forward raw subprocess logs or private audio content to WeChat.
        raise VoiceError("Voice processing failed") from exc


def reply_mode(text: str, prefer_voice: bool) -> tuple[str, bool]:
    modes = REPLY_MARKER.findall(text)
    if modes:
        prefer_voice = modes[-1] == "voice"
    return REPLY_MARKER.sub("", text).strip(), prefer_voice


def explicit_text_reply(text: str) -> bool:
    # A direct formatting request must still work if a model forgets its
    # channel marker. Restrict this fallback to clear requests at the start;
    # quoted examples and negated "不要用文字" remain for model interpretation.
    return bool(
        re.match(
            r"^\s*(?:(?:请|麻烦|这次|先|以后|后面|接下来)[，,、\s]*)*"
            r"(?:(?:用|改用|改成|换成)文字(?:来)?(?:回复|回答|说|告诉)|"
            r"(?:文字|打字)(?:回复|回答)|(?:不要|不用|别)(?:再)?(?:用|发)?语音)",
            text,
        )
    )


def voice_metadata(path: Path, root: Path) -> dict:
    resolved = path.resolve()
    if not resolved.is_relative_to(root.resolve()) or resolved.suffix != ".silk":
        raise ValueError("Voice is outside the bridge's generated voice directory")
    if not 0 < resolved.stat().st_size <= MAX_VOICE_BYTES:
        raise ValueError("Voice file is empty or oversized")
    data = resolved.read_bytes()
    if not data.startswith((b"\x02#!SILK_V3", b"#!SILK_V3")):
        raise ValueError("Voice file has no valid SILK header")
    manifest = resolved.with_suffix(".json").resolve()
    if not manifest.is_relative_to(root.resolve()) or manifest.stat().st_size > 4096:
        raise ValueError("Voice metadata is outside the media directory or oversized")
    metadata = json.loads(manifest.read_text(encoding="utf-8"))
    if not isinstance(metadata, dict) or not isinstance(metadata.get("playtime"), int):
        raise ValueError("Voice playback metadata is not valid")
    if (
        metadata.get("sha256") != hashlib.sha256(data).hexdigest()
        or metadata.get("encode_type") != 6
        or metadata.get("sample_rate") != 24000
        or metadata.get("bits_per_sample") != 16
        or not 0 < metadata.get("playtime", 0) <= 60000
    ):
        raise ValueError("Voice playback metadata is invalid")
    return {
        key: metadata[key]
        for key in ("encode_type", "sample_rate", "bits_per_sample", "playtime")
    }


def voice_attachment(path: Path, root: Path) -> bytes:
    resolved = path.resolve()
    if not resolved.is_relative_to(root.resolve()) or resolved.suffix != ".mp3":
        raise ValueError("Audio is outside the bridge's generated voice directory")
    if not 0 < resolved.stat().st_size <= MAX_VOICE_BYTES:
        raise ValueError("Audio file is empty or oversized")
    data = resolved.read_bytes()
    if not (
        data.startswith(b"ID3")
        or (len(data) >= 2 and data[0] == 0xFF and data[1] & 0xE0 == 0xE0)
    ):
        raise ValueError("Audio file has no valid MP3 header")
    return data


class Voices:
    def __init__(self, config: dict, root: Path) -> None:
        self.config = config
        self.root = root.resolve()

    @property
    def incoming_root(self) -> Path:
        return (Path(self.config["workspace"]) / ".wechat-voices").resolve()

    def incoming(self, message_id: str) -> tuple[str, str]:
        try:
            database = (Path(self.config["data_path"]) / "messages.db").resolve()
            with closing(
                sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)
            ) as db:
                row = db.execute(
                    "SELECT data FROM messages WHERE message_id=? AND user_id=? AND direction=1",
                    (message_id, self.config["owner_user_id"]),
                ).fetchone()
            if row is None:
                raise ValueError("Voice is not in the owner's message store")
            message = deserialize_message(row[0])
            if (
                str(message.message_id) != message_id
                or message.from_user != self.config["owner_user_id"]
                or message.msg_type != MessageType.VOICE
                or message.voice is None
            ):
                raise ValueError("Stored message is not an owner-supplied voice")
            client = WeiLink(
                base_path=Path(self.config["data_path"]), message_store=False
            )
            try:
                data = client.download(message)
            finally:
                client.close()
            if not data or len(data) > MAX_VOICE_BYTES:
                raise ValueError("Incoming voice is empty or oversized")
            self.incoming_root.mkdir(parents=True, exist_ok=True)
            path = self.incoming_root / (hashlib.sha256(data).hexdigest() + ".audio")
            path.write_bytes(data)
            result = run_speech("--transcribe", str(path))
            text = result["text"].strip()
            if not text:
                raise ValueError("No speech could be recognized")
            return str(path), text
        except (OSError, ValueError, KeyError, TypeError, sqlite3.Error) as exc:
            raise VoiceError("Incoming voice could not be recognized") from exc

    def outgoing(self, text: str) -> tuple[list[str], bool]:
        # Keep copyable code and links as text alongside the spoken reply.
        keep_text = bool(re.search(r"```|https?://", text))
        spoken = re.sub(r"```.*?```", "", text, flags=re.DOTALL)
        spoken = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", spoken)
        spoken = re.sub(r"https?://\S+", "链接见文字消息", spoken)
        spoken = re.sub(r"(?m)^\s*#{1,6}\s+", "", spoken)
        spoken = spoken.replace("**", "").replace("`", "").strip()
        if not spoken:
            spoken, keep_text = "需要复制的内容已经放在文字消息里。", True
        self.root.mkdir(parents=True, exist_ok=True)
        # iLink can accept native VOICE while personal WeChat renders nothing
        # (Tencent/openclaw-weixin#91). Use a playable FILE attachment by default;
        # an API success response cannot establish native voice delivery.
        result = run_speech(
            "--text",
            spoken,
            "--output",
            str(self.root / f"{uuid.uuid4().hex}.mp3"),
        )
        paths = result.get("paths", [])
        if not paths:
            raise VoiceError("No voice was generated")
        for path in paths:
            voice_attachment(Path(path), self.root)
        return paths, keep_text
