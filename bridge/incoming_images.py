"""Download owner-supplied WeChat images without taking over the receiver."""

from __future__ import annotations

import hashlib
import os
import sqlite3
import tempfile
from contextlib import closing
from io import BytesIO
from pathlib import Path

from PIL import Image
from weilink import WeiLink
from weilink._store import deserialize_message
from weilink.models import MessageType

MAX_IMAGE_BYTES = 20 * 1024 * 1024


class IncomingImageError(RuntimeError):
    pass


class IncomingImages:
    def __init__(self, config: dict) -> None:
        self.config = config

    @property
    def root(self) -> Path:
        return (Path(self.config["workspace"]) / ".wechat-images").resolve()

    def message(self, message_id: str):
        # The SDK already persists CDN/decryption metadata before receive()
        # returns. Reuse that record by exact ID and owner after a restart;
        # do not copy media URLs, keys or context tokens into model input/logs.
        database = (Path(self.config["data_path"]) / "messages.db").resolve()
        with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as db:
            row = db.execute(
                "SELECT data FROM messages WHERE message_id=? AND user_id=? AND direction=1",
                (message_id, self.config["owner_user_id"]),
            ).fetchone()
        if row is None:
            raise ValueError("Incoming image is not in the owner's message store")
        message = deserialize_message(row[0])
        if (
            str(message.message_id) != message_id
            or message.from_user != self.config["owner_user_id"]
            or message.msg_type != MessageType.IMAGE
            or message.image is None
        ):
            raise ValueError("Stored message is not an owner-supplied image")
        return message

    @staticmethod
    def validate(data: bytes) -> str:
        if not data or len(data) > MAX_IMAGE_BYTES:
            raise ValueError("Incoming image exceeds the size limit or is empty")
        with Image.open(BytesIO(data)) as image:
            suffix = {
                "PNG": ".png",
                "JPEG": ".jpg",
                "WEBP": ".webp",
                "GIF": ".gif",
            }.get(image.format)
            if suffix is None or image.width * image.height > 40_000_000:
                raise ValueError("Incoming image format or dimensions are unsupported")
            image.verify()
        return suffix

    def prepare(self, message_id: str, cached_path: str | None = None) -> str:
        try:
            if cached_path:
                cached = Path(cached_path).resolve()
                if not cached.is_relative_to(self.root):
                    raise ValueError(
                        "Cached image is outside the incoming image directory"
                    )
                if cached.is_file():
                    self.validate(cached.read_bytes())
                    return str(cached)
            message = self.message(message_id)
            # A dedicated download-only client never calls recv(), preserving
            # the bridge's single long-poll owner and message ordering.
            client = WeiLink(
                base_path=Path(self.config["data_path"]), message_store=False
            )
            try:
                data = client.download(message)
            finally:
                client.close()
            suffix = self.validate(data)
            if suffix == ".gif":
                # Model image input is static. Decode the first frame rather
                # than passing an unsupported animation to the provider.
                with Image.open(BytesIO(data)) as image:
                    output = BytesIO()
                    image.convert("RGBA").save(output, "PNG")
                    data = output.getvalue()
                suffix = self.validate(data)
            self.root.mkdir(parents=True, exist_ok=True)
            target = self.root / (hashlib.sha256(data).hexdigest() + suffix)
            temporary = None
            try:
                with tempfile.NamedTemporaryFile(dir=self.root, delete=False) as stream:
                    temporary = Path(stream.name)
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, target)
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
            return str(target)
        except (
            OSError,
            ValueError,
            KeyError,
            TypeError,
            sqlite3.Error,
            Image.DecompressionBombError,
        ) as exc:
            raise IncomingImageError(
                "Incoming image could not be downloaded or decoded"
            ) from exc
