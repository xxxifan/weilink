"""Reuse the existing WeiLink credentials; keep one live receiving owner."""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from contextvars import ContextVar
from io import BytesIO
from pathlib import Path

from PIL import Image
from weilink import _protocol as proto

from quoted_messages import QuotedWeiLink, capture_sent_message
from voice import voice_attachment, voice_metadata

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "bridge-config.json"


def load_config(path: Path = CONFIG_PATH) -> dict:
    config = json.loads(path.read_text(encoding="utf-8"))
    command = list(config["codex_command"])
    for field, option in (
        ("model", "model"),
        ("reasoning_effort", "model_reasoning_effort"),
    ):
        if config.get(field):
            command.extend(["-c", f"{option}={json.dumps(config[field])}"])
    config["codex_command"] = command
    return config


_post = proto.post
_voice_metadata = ContextVar("voice_metadata", default=None)


def checked_post(*args, **kwargs):
    endpoint = args[0] if args else kwargs.get("endpoint")
    body = args[1] if len(args) > 1 else kwargs.get("body", {})
    metadata = _voice_metadata.get()
    if metadata and endpoint == proto.EP_SEND_MESSAGE:
        # WeiLink 0.6.2 uploads voice bytes but omits playback fields. Keep
        # metadata local to this synchronous send, including concurrent texts.
        for item in body.get("msg", {}).get("item_list", []):
            if item.get("type") == 3 and "voice_item" in item:
                item["voice_item"].update(metadata)
    result = _post(*args, **kwargs)
    # WeiLink 0.6.2 only logs most nonzero business codes. A bridge must
    # not acknowledge delivery or advance work after that kind of failure.
    if result.get("ret") not in (None, 0) or result.get("errcode") not in (None, 0):
        raise proto.ILinkError(
            ret=result.get("ret", -1),
            errcode=result.get("errcode"),
            errmsg=result.get("errmsg", "WeChat API rejected the request"),
        )
    capture_sent_message(endpoint, body, result)
    return result


proto.post = checked_post


def send_text(config: dict, text: str) -> bool:
    # Separate handles avoid sharing a Windows byte-range FileLock between
    # the receiving thread and the sending thread. State is reread from disk.
    client = QuotedWeiLink(base_path=Path(config["data_path"]), message_store=True)
    try:
        return bool(client.send(config["owner_user_id"], text, auto_recv=False))
    finally:
        client.close()


def send_image(config: dict, path: str) -> bool:
    # Only installed packs and validated, bridge-persisted generated rasters
    # enter this sender. Model text cannot nominate arbitrary local files.
    image = Path(path).resolve()
    roots = (ROOT / "personas" / "sources", ROOT / "data" / "generated-images")
    if not any(image.is_relative_to(root.resolve()) for root in roots):
        raise ValueError("Image is outside the bridge media directories")
    data = image.read_bytes()
    media = {"image": data}
    if config.get("animated_meme_mode", "image") == "file":
        with Image.open(BytesIO(data)) as source:
            if getattr(source, "is_animated", False):
                # iLink image messages can display only a still even when the
                # uploaded bytes are a GIF. Attach the original animation as
                # a file; do not re-encode it or lose frames/timing. Some
                # upstream GIFs have a .jpg suffix, so use the detected format.
                suffix = {"GIF": ".gif", "WEBP": ".webp", "PNG": ".png"}.get(
                    source.format, image.suffix
                )
                media = {"file": data, "file_name": image.with_suffix(suffix).name}
    client = QuotedWeiLink(base_path=Path(config["data_path"]), message_store=True)
    try:
        return bool(client.send(config["owner_user_id"], **media, auto_recv=False))
    finally:
        client.close()


def send_voice(config: dict, path: str) -> bool:
    audio = Path(path).resolve()
    if audio.suffix == ".mp3":
        data = voice_attachment(audio, ROOT / "data" / "generated-voices")
        client = QuotedWeiLink(base_path=Path(config["data_path"]), message_store=True)
        try:
            return bool(
                client.send(
                    config["owner_user_id"],
                    file=data,
                    file_name="晓伊语音.mp3",
                    auto_recv=False,
                )
            )
        finally:
            client.close()
    metadata = voice_metadata(audio, ROOT / "data" / "generated-voices")
    client = QuotedWeiLink(base_path=Path(config["data_path"]), message_store=True)
    token = _voice_metadata.set(metadata)
    try:
        return bool(
            client.send(
                config["owner_user_id"], voice=audio.read_bytes(), auto_recv=False
            )
        )
    finally:
        _voice_metadata.reset(token)
        client.close()


def _message_blocks(text: str) -> Iterator[str]:
    current: list[str] = []
    fence = None
    for line in text.splitlines(keepends=True):
        content = line.rstrip("\r\n")
        if fence:
            current.append(line)
            if re.fullmatch(
                rf" {{0,3}}{re.escape(fence[0])}{{{len(fence)},}}[ \t]*", content
            ):
                # A trailing newline must not push an otherwise fitting code
                # block over the limit and separate its closing fence.
                current[-1] = content
                yield "".join(current)
                current = [line[len(content) :]] if len(line) > len(content) else []
                fence = None
            continue
        opening = re.fullmatch(r" {0,3}(`{3,}|~{3,})(.*)", content)
        if opening and not (opening[1].startswith("`") and "`" in opening[2]):
            if current:
                yield "".join(current)
            current, fence = [line], opening[1]
        else:
            current.append(line)
            if not content.strip():
                yield "".join(current)
                current = []
    if current:
        yield "".join(current)


def _split_long_block(text: str, byte_limit: int) -> Iterator[str]:
    data = text.encode("utf-8")
    offset = 0
    while offset < len(data):
        # Only a partial trailing UTF-8 character is dropped here; it remains
        # in data and starts the next part. Never rewrite code or whitespace.
        part = data[offset : offset + byte_limit].decode("utf-8", errors="ignore")
        if not part:
            raise ValueError("byte_limit is too small for a UTF-8 character")
        if offset + len(part.encode("utf-8")) < len(data):
            newline = part.rfind("\n") + 1
            if newline > len(part) // 2:
                part = part[:newline]
        yield part
        offset += len(part.encode("utf-8"))


def split_text(text: str, byte_limit: int = 10000) -> list[str]:
    if byte_limit <= 0:
        raise ValueError("byte_limit must be positive")
    parts: list[str] = []
    current = ""
    size = 0
    # Keep paragraphs and fenced command/code blocks intact whenever they fit
    # one message. Oversized blocks still preserve every original character.
    for block in _message_blocks(text):
        width = len(block.encode("utf-8"))
        if current and size + width > byte_limit:
            parts.append(current)
            current, size = "", 0
        if width > byte_limit:
            chunks = list(_split_long_block(block, byte_limit))
            parts.extend(chunks[:-1])
            current, size = chunks[-1], len(chunks[-1].encode("utf-8"))
        else:
            current += block
            size += width
    if current:
        parts.append(current)
    return parts or [""]
