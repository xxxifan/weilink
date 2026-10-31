"""Reuse the existing WeiLink credentials; keep one live receiving owner."""

from __future__ import annotations

import json
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


def split_text(text: str, byte_limit: int = 10000) -> list[str]:
    parts: list[str] = []
    current: list[str] = []
    size = 0
    for char in text:
        width = len(char.encode("utf-8"))
        if size + width > byte_limit and current:
            parts.append("".join(current))
            current, size = [], 0
        current.append(char)
        size += width
    if current:
        parts.append("".join(current))
    return parts or [""]
