"""Best-effort WeChat presence, kept separate from durable reply delivery."""

from __future__ import annotations

import asyncio
import json
import logging
import time
import urllib.request
from pathlib import Path

from weilink import WeiLink
from weilink import _protocol as proto

log = logging.getLogger(__name__)

START = "/ilink/bot/msg/notifystart"
STOP = "/ilink/bot/msg/notifystop"
BASE_INFO = {
    "channel_version": proto.CHANNEL_VERSION,
    "bot_agent": "WeiLinkCodex/1.0.0",
}


class StatusTransport:
    def __init__(self, config: dict, clock=time.monotonic) -> None:
        self.config = config
        self.clock = clock
        self.tickets: dict[tuple, tuple[str, float]] = {}

    @staticmethod
    def _request(info, endpoint: str, body: dict) -> dict:
        request = urllib.request.Request(
            f"{info.base_url.rstrip('/')}{endpoint}",
            data=json.dumps({**body, "base_info": BASE_INFO}).encode("utf-8"),
            headers=proto._make_headers(info.token),
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            raw = response.read()
        # Official sendTyping consumes no response body. Unlike the SDK's
        # general post(), accept an empty HTTP success for this endpoint only.
        result = (
            {}
            if not raw.strip() and endpoint == proto.EP_SEND_TYPING
            else json.loads(raw)
        )
        if not isinstance(result, dict):
            raise ValueError("Invalid status response")
        if result.get("ret") not in (None, 0) or result.get("errcode") not in (None, 0):
            error = (
                proto.SessionExpiredError
                if result.get("errcode") == proto.SESSION_EXPIRED
                else proto.ILinkError
            )
            raise error(
                ret=result.get("ret", -1),
                errcode=result.get("errcode"),
                errmsg="WeChat status request rejected",
            )
        return result

    def _session(self, client):
        session = client._find_session_for_user(self.config["owner_user_id"])
        if session is not None and session.bot_info is not None:
            return session
        # A freshly logged-in single account may not yet have a user context.
        # Never notify an arbitrary account when several accounts are logged in.
        logged_in = [
            session for session in client._sessions.values() if session.bot_info
        ]
        return logged_in[0] if len(logged_in) == 1 else None

    def notify(self, starting: bool) -> bool:
        client = WeiLink(base_path=Path(self.config["data_path"]))
        try:
            session = self._session(client)
            if session is None:
                return False
            self._request(session.bot_info, START if starting else STOP, {})
            return True
        finally:
            client.close()

    def typing(self, active: bool) -> bool:
        # A fresh handle reads newly received contexts and renewed credentials
        # without sharing the receiving client's Windows FileLock across threads.
        client = WeiLink(base_path=Path(self.config["data_path"]))
        try:
            session = self._session(client)
            if session is None:
                return False
            info = session.bot_info
            owner = self.config["owner_user_id"]
            key = (info.bot_id, info.base_url, info.token, owner)
            cached = self.tickets.get(key)
            try:
                if cached is None or cached[1] <= self.clock():
                    body = {"ilink_user_id": owner}
                    if context := session.context_tokens.get(owner):
                        body["context_token"] = context
                    result = self._request(info, proto.EP_GET_CONFIG, body)
                    ticket = result.get("typing_ticket")
                    if not isinstance(ticket, str) or not ticket:
                        return False
                    cached = (ticket, self.clock() + 3600)
                    self.tickets = {key: cached}
                self._request(
                    info,
                    proto.EP_SEND_TYPING,
                    {
                        "ilink_user_id": owner,
                        "typing_ticket": cached[0],
                        "status": 1 if active else 2,
                    },
                )
                return True
            except Exception:
                # A rejected/expired ticket must be fetched again after backoff.
                self.tickets.pop(key, None)
                raise
        finally:
            client.close()


class DeliveryStatus:
    def __init__(self, transport, clock=time.monotonic) -> None:
        self.transport = transport
        self.clock = clock
        self.online_notified = False
        self.typing: bool | None = None
        self.desired = False
        self.lifecycle_error: str | None = None
        self.typing_error: str | None = None
        self._notify_at = 0.0
        self._typing_at = 0.0
        self._notify_failures = 0
        self._typing_failures = 0

    async def _call(self, operation: str, value: bool) -> str | None:
        try:
            if await asyncio.to_thread(getattr(self.transport, operation), value):
                return None
            return "Unavailable"
        except Exception as exc:
            # Peer IDs, tickets and credentials must not appear in status files
            # or logs; presence errors must never fail or retry a text reply.
            return type(exc).__name__

    @staticmethod
    def _backoff(failures: int) -> float:
        return min(5 * 2 ** min(failures - 1, 4), 60)

    async def step(self, desired: bool) -> None:
        if not self.online_notified and self.clock() >= self._notify_at:
            error = await self._call("notify", True)
            if error != self.lifecycle_error:
                log.warning("WeChat start notification: %s", error or "recovered")
            self.lifecycle_error = error
            self.online_notified = error is None
            if self.online_notified:
                log.info("WeChat client start acknowledged")
            self._notify_failures += 1
            self._notify_at = self.clock() + self._backoff(self._notify_failures)

        if desired != self.desired:
            self.desired = desired
            self._typing_at = 0.0
            self._typing_failures = 0
        if self.clock() < self._typing_at or (not desired and self.typing is False):
            return
        started_at = self.clock()
        error = await self._call("typing", desired)
        if error != self.typing_error:
            log.warning("WeChat typing status: %s", error or "recovered")
        self.typing_error = error
        if error is None and self.typing != desired:
            log.info("WeChat typing acknowledged: %s", "active" if desired else "idle")
        self.typing = desired if error is None else None
        self._typing_failures = self._typing_failures + 1 if error else 0
        # Match the official client's five-second typing keepalive. Idle only
        # sends cancel on transition (or once at startup to clear stale typing).
        self._typing_at = (
            self.clock() + self._backoff(self._typing_failures)
            if error
            else started_at + 5
        )

    def snapshot(self) -> dict:
        return {
            "online_notified": self.online_notified,
            "typing": self.typing,
            "typing_desired": self.desired,
            "typing_error": self.typing_error,
            "lifecycle_error": self.lifecycle_error,
        }

    async def run(self, desired, stopping: asyncio.Event) -> None:
        try:
            while not stopping.is_set():
                await self.step(desired())
                if desired() != self.desired:
                    continue
                try:
                    await asyncio.wait_for(stopping.wait(), 0.5)
                except TimeoutError:
                    pass
        finally:
            # Serialize shutdown after any in-flight HTTP call. Cancelling a
            # to_thread task would leave its request running and could re-enable
            # typing after cancellation. The bridge waits for this worker.
            self.desired = False
            if self.typing is not False:
                self.typing_error = await self._call("typing", False)
                self.typing = False if self.typing_error is None else None
            self.lifecycle_error = await self._call("notify", False)
            if self.lifecycle_error is None:
                self.online_notified = False
                log.info("WeChat client stop acknowledged")
            else:
                log.warning("WeChat stop notification: %s", self.lifecycle_error)
