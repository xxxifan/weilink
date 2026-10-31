"""Persist generated rasters from the active turn before queuing delivery."""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import tempfile
import time
from io import BytesIO
from pathlib import Path

from PIL import Image

log = logging.getLogger(__name__)
IMAGE_TYPES = {"imageGeneration", "image_generation_call"}
MAX_IMAGE_BYTES = 20 * 1024 * 1024


class GeneratedImages:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()

    @staticmethod
    def checkpoint(path: str | None) -> int:
        try:
            return Path(path).stat().st_size if path else 0
        except OSError:
            return 0

    @staticmethod
    def rollout_items(path: str | None, turn_id: str, offset: int) -> list[dict]:
        if not path:
            return []
        items = []
        current_turn = None
        # Some App Server versions omit image_generation_call from public
        # thread items. Read only newly appended records for this exact turn;
        # never resend images from a previous turn or another conversation.
        with Path(path).open("rb") as stream:
            stream.seek(offset)
            deadline = time.monotonic() + 1
            while True:
                position = stream.tell()
                line = stream.readline()
                if not line.endswith(b"\n"):
                    # The completed notification and rollout flush can race.
                    # Retry the same offset so a partially written JSON line
                    # is not silently discarded, with a bounded reader wait.
                    if time.monotonic() >= deadline:
                        break
                    stream.seek(position)
                    time.sleep(0.05)
                    continue
                try:
                    record = json.loads(line)
                except (ValueError, UnicodeDecodeError):
                    continue
                payload = record.get("payload", {})
                kind = record.get("type")
                if kind == "turn_context" or (
                    kind == "event_msg" and payload.get("type") == "task_started"
                ):
                    current_turn = payload.get("turn_id")
                if (
                    current_turn == turn_id
                    and kind == "response_item"
                    and payload.get("type") in IMAGE_TYPES
                ):
                    items.append(payload)
                if (
                    kind == "event_msg"
                    and payload.get("type") == "task_complete"
                    and payload.get("turn_id") == turn_id
                ):
                    break
        return items

    def save(self, item: dict) -> Path:
        result = item.get("result")
        if not isinstance(result, str) or not result:
            raise ValueError("Generated image has no inline payload")
        if result.startswith("data:"):
            header, result = result.split(",", 1)
            if not header.startswith("data:image/") or not header.endswith(";base64"):
                raise ValueError("Invalid generated image data URL")
        if len(result) > (MAX_IMAGE_BYTES + 2) // 3 * 4:
            raise ValueError("Generated image exceeds the delivery size limit")
        data = base64.b64decode(result, validate=True)
        with Image.open(BytesIO(data)) as image:
            suffix = {"PNG": ".png", "JPEG": ".jpg", "WEBP": ".webp"}.get(image.format)
            if suffix is None:
                raise ValueError("Unsupported generated image format")
            image.verify()
        self.root.mkdir(parents=True, exist_ok=True)
        target = self.root / (hashlib.sha256(data).hexdigest() + suffix)
        # Persist complete bytes atomically before committing the outbox row.
        # Content names deduplicate event/rollout copies and survive restarts.
        with tempfile.NamedTemporaryFile(dir=self.root, delete=False) as stream:
            temporary = Path(stream.name)
            try:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            except BaseException:
                stream.close()
                temporary.unlink(missing_ok=True)
                raise
        try:
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
        return target

    def collect(
        self,
        items: list[dict],
        path: str | None,
        turn_id: str,
        offset: int,
    ) -> tuple[list[str], bool]:
        candidates = [item for item in items if item.get("type") in IMAGE_TYPES]
        failed = False
        try:
            candidates.extend(self.rollout_items(path, turn_id, offset))
        except OSError:
            log.warning("Generated image rollout could not be read")
            failed = bool(candidates)
        paths = []
        for item in candidates:
            if item.get("status") != "completed" or item.get("failure"):
                failed = True
                continue
            try:
                saved = str(self.save(item))
            except (OSError, ValueError, Image.DecompressionBombError):
                log.warning("Generated image could not be persisted")
                failed = True
                continue
            if saved not in paths:
                paths.append(saved)
        return paths, failed
