"""Retain sent image bytes and prepare referenced images as real model input."""

from __future__ import annotations

import hashlib
import os
import sqlite3
import tempfile
from contextlib import closing
from dataclasses import replace
from io import BytesIO
from pathlib import Path

from PIL import Image
from weilink import WeiLink
from weilink._store import deserialize_message
from weilink.models import MessageType

from incoming_images import IncomingImageError, IncomingImages
from quote_store import QuoteStore


class QuoteImageCache:
    def __init__(self, data_root: Path) -> None:
        self.root = (data_root / "quote-images").resolve()

    def save(self, data: bytes) -> str:
        suffix = IncomingImages.validate(data)
        if suffix == ".gif":
            with Image.open(BytesIO(data)) as image:
                output = BytesIO()
                image.convert("RGBA").save(output, "PNG")
                data = output.getvalue()
            suffix = IncomingImages.validate(data)
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


class QuotedImages:
    def __init__(self, config: dict) -> None:
        self.config = config
        self.data_root = Path(
            config.get("data_path", Path(__file__).resolve().parent.parent / "data")
        )
        self.cache = QuoteImageCache(self.data_root)

    @property
    def incoming_root(self) -> Path:
        # Status-only bridge clients do not need a media workspace. Resolve
        # it only when an actual quoted image is being prepared.
        return (Path(self.config["workspace"]) / ".wechat-images").resolve()

    def stored_message(self, message_id):
        database = (self.data_root / "messages.db").resolve()
        with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as db:
            row = db.execute(
                "SELECT data FROM messages WHERE CAST(message_id AS TEXT)=? AND user_id=? AND direction=1",
                (str(message_id), self.config["owner_user_id"]),
            ).fetchone()
        if row is None:
            return None
        message = deserialize_message(row[0])
        if (
            str(message.message_id) != str(message_id)
            or message.from_user != self.config["owner_user_id"]
        ):
            raise ValueError("Referenced media does not belong to the owner")
        return message

    def prepare(self, message_id: str, reference: dict) -> str:
        try:
            cached_path = reference.get("image_path")
            if cached_path:
                cached = Path(cached_path).resolve()
                if not any(
                    cached.is_relative_to(root)
                    for root in (self.cache.root, self.incoming_root)
                ):
                    raise ValueError(
                        "Referenced image is outside managed image directories"
                    )
                if cached.is_file():
                    IncomingImages.validate(cached.read_bytes())
                    return str(cached)

            # The quoted CDN metadata is already stored privately by the SDK.
            # Read exact owner records rather than putting URLs/keys in prompts.
            original = self.stored_message(
                reference.get("source_message_id") or reference.get("message_id")
            )
            if (
                original is not None
                and original.msg_type == MessageType.IMAGE
                and original.image is not None
            ):
                download_message = original
            else:
                containing = self.stored_message(message_id)
                quoted = containing.ref_msg if containing is not None else None
                if quoted is None or quoted.image is None:
                    raise ValueError(
                        "Quoted image bytes and download metadata are unavailable"
                    )
                download_message = replace(
                    containing,
                    msg_type=MessageType.IMAGE,
                    image=quoted.image,
                    text=None,
                    ref_msg=None,
                )

            client = WeiLink(base_path=self.data_root, message_store=False)
            try:
                data = client.download(download_message)
            finally:
                client.close()
            path = self.cache.save(data)
            # This cache update is optional; a cache write failure must not
            # hide an image that has already been downloaded successfully.
            try:
                QuoteStore(self.data_root / "quote-messages.sqlite").put(
                    download_message.bot_id,
                    self.config["owner_user_id"],
                    reference.get("message_id"),
                    {**reference, "image_path": path},
                )
            except (OSError, sqlite3.Error, ValueError):
                pass
            return path
        except (
            OSError,
            sqlite3.Error,
            ValueError,
            TypeError,
            KeyError,
            Image.DecompressionBombError,
        ) as exc:
            raise IncomingImageError(
                "Quoted image could not be downloaded or decoded"
            ) from exc
