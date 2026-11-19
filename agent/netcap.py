"""Network capture through CDP, scoped to the agent browser only.

Sensitive headers are redacted. Response bodies are never stored unless
the user approves it for the session.
"""

from __future__ import annotations

import base64
import time
from pathlib import Path

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page

from .config import CAPTURE_DIR, assert_writable, cfg, get_logger
from .notify import Notifier
from .store import Store

log = get_logger("netcap")
REDACT_HEADERS = {"authorization", "cookie", "set-cookie", "proxy-authorization", "x-api-key", "x-auth-token"}
TEXTUAL_MIME = ("json", "text", "javascript", "xml", "html")


def _redact(headers: dict) -> dict:
    return {k: ("***" if k.lower() in REDACT_HEADERS else v) for k, v in (headers or {}).items()}


class NetworkCapture:
    def __init__(self, store: Store, notifier: Notifier, run_id: int | None = None,
                 capture_bodies: bool = False):
        self.store = store
        self.notifier = notifier
        self.run_id = run_id
        self.capture_bodies = capture_bodies
        self.entries: list[dict] = []
        self._sessions: dict[Page, object] = {}
        self._pending: dict[str, dict] = {}
        self._bodies_allowed = False
        self._body_dir: Path | None = None

    def attach(self, page: Page) -> None:
        if self.capture_bodies and not self._bodies_allowed:
            self._bodies_allowed = self.notifier.approval(
                "netcap_bodies",
                "متن پاسخ‌های شبکه روی D:\\Work\\agent-state\\captures ذخیره شود؟",
            )
            if self._bodies_allowed:
                self._body_dir = assert_writable(CAPTURE_DIR / str(int(time.time())))
                self._body_dir.mkdir(parents=True, exist_ok=True)
        try:
            session = page.context.new_cdp_session(page)
            session.send("Network.enable", {})
            session.on("Network.requestWillBeSent", self._on_request)
            session.on("Network.responseReceived", self._on_response)
            session.on("Network.loadingFinished", lambda p: self._on_finished(p, session))
            session.on("Network.loadingFailed", self._on_failed)
        except PlaywrightError as exc:
            log.warning("CDP attach failed: %s", exc)
            return
        self._sessions[page] = session

    def detach(self) -> None:
        for session in self._sessions.values():
            try:
                session.detach()
            except PlaywrightError:
                pass
        self._sessions.clear()

    def _on_request(self, params: dict) -> None:
        request = params.get("request", {})
        self._pending[params.get("requestId", "")] = {
            "method": request.get("method", "GET"),
            "url": request.get("url", ""),
            "status": 0, "mime": "", "size": 0, "headers": {},
        }

    def _on_response(self, params: dict) -> None:
        entry = self._pending.get(params.get("requestId", ""))
        if entry is None:
            return
        response = params.get("response", {})
        entry["status"] = int(response.get("status", 0))
        entry["mime"] = str(response.get("mimeType", ""))
        entry["headers"] = _redact(response.get("headers", {}))

    def _on_finished(self, params: dict, session) -> None:
        request_id = params.get("requestId", "")
        entry = self._pending.pop(request_id, None)
        if entry is None:
            return
        entry["size"] = int(params.get("encodedDataLength", 0))
        if self._bodies_allowed and self._body_dir is not None:
            self._save_body(session, request_id, entry)
        self._commit(entry)

    def _on_failed(self, params: dict) -> None:
        entry = self._pending.pop(params.get("requestId", ""), None)
        if entry is None:
            return
        entry["status"] = -1
        entry["mime"] = params.get("errorText", "failed")
        self._commit(entry)

    def _save_body(self, session, request_id: str, entry: dict) -> None:
        mime = entry["mime"].lower()
        if not any(token in mime for token in TEXTUAL_MIME):
            return
        if entry["size"] > int(cfg("netcap_body_max_bytes")):
            return
        try:
            result = session.send("Network.getResponseBody", {"requestId": request_id})
        except PlaywrightError:
            return
        raw = result.get("body", "")
        data = base64.b64decode(raw) if result.get("base64Encoded") else raw.encode("utf-8")
        target = assert_writable(self._body_dir / f"{request_id.replace('.', '_')}.txt")
        try:
            target.write_bytes(data[: int(cfg("netcap_body_max_bytes"))])
            entry["body_path"] = str(target)
        except OSError as exc:
            log.warning("body save failed: %s", exc)

    def _commit(self, entry: dict) -> None:
        if entry["url"].startswith("data:"):
            return
        self.entries.append(entry)
        self.store.add_capture(
            self.run_id, entry["method"], entry["url"], entry["status"],
            entry["mime"], entry["size"], entry["headers"],
        )
        if entry["status"] >= 400 or entry["status"] == -1:
            self.notifier.emit("net_error", {"url": entry["url"], "status": entry["status"]})