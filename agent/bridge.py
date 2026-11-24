"""Local-only HTTP bridge between the Python engine and the AHK front-end.

Bound to loopback and guarded by a shared token: any local process could
otherwise drive the browser through this port.
"""

from __future__ import annotations

import hmac
import json
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.error import URLError
from urllib.parse import parse_qs, urlsplit
from urllib.request import Request, urlopen

from .config import TOKEN_PATH, VERSION, cfg, ensure_dirs, get_logger
from .store import Store

log = get_logger("bridge")
TOKEN_HEADER = "X-Agent-Token"


def ensure_token() -> str:
    ensure_dirs()
    if TOKEN_PATH.exists():
        token = TOKEN_PATH.read_text(encoding="utf-8").strip()
        if token:
            return token
    token = secrets.token_urlsafe(32)
    TOKEN_PATH.write_text(token, encoding="utf-8")
    return token


class _Handler(BaseHTTPRequestHandler):
    bridge: "BridgeServer"
    server_version = "AgentBridge/" + VERSION
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: Any) -> None:
        log.debug("%s - %s", self.address_string(), fmt % args)

    def _send(self, code: int, obj: dict) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self) -> bool:
        supplied = self.headers.get(TOKEN_HEADER, "")
        if hmac.compare_digest(supplied, self.bridge.token):
            return True
        self._send(401, {"ok": False, "error": "token namotabar"})
        return False

    def _json_body(self) -> dict | None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._send(400, {"ok": False, "error": "Content-Length namotabar"})
            return None
        if length <= 0 or length > 4_000_000:
            self._send(400, {"ok": False, "error": "badane khali ya bozorg"})
            return None
        try:
            data = json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            self._send(400, {"ok": False, "error": "JSON namotabar"})
            return None
        if not isinstance(data, dict):
            self._send(400, {"ok": False, "error": "JSON bayad object bashad"})
            return None
        return data

    def do_GET(self) -> None:  # noqa: N802 - stdlib naming
        if not self._authorized():
            return
        parts = urlsplit(self.path)
        query = parse_qs(parts.query)
        route = parts.path.rstrip("/") or "/"
        store = self.bridge.store
        try:
            if route == "/health":
                self._send(200, {"ok": True, "version": VERSION, "engine": self.bridge.engine_alive()})
            elif route == "/events":
                since = int(query.get("since", ["0"])[0])
                limit = min(int(query.get("limit", ["200"])[0]), 500)
                self._send(200, {"ok": True, "events": store.events_since(since, limit)})
            elif route == "/cmd/next":
                self._send(200, {"ok": True, "command": store.next_command()})
            elif route == "/approvals":
                self._send(200, {"ok": True, "approvals": store.pending_approvals()})
            elif route == "/ahk/poll":
                since = int(query.get("since", ["0"])[0])
                lines = ["H %d" % (1 if self.bridge.engine_alive() else 0)]
                for event in store.events_since(since, 100):
                    lines.append("E %d %s %s" % (event["id"], event["kind"],
                                                 _ahk_summary(event["kind"], event["payload"])))
                for approval in store.pending_approvals():
                    lines.append("A %d %s %s" % (approval["id"], approval["kind"],
                                                 _ahk_clean(approval["summary"])))
                body = "\n".join(lines).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif route.startswith("/approvals/"):
                approval = store.approval(int(route.rsplit("/", 1)[1]))
                if approval is None:
                    self._send(404, {"ok": False, "error": "peyda nashod"})
                else:
                    self._send(200, {"ok": True, "approval": approval})
            else:
                self._send(404, {"ok": False, "error": "masir nashenas"})
        except (ValueError, IndexError):
            self._send(400, {"ok": False, "error": "parameter namotabar"})

    def do_POST(self) -> None:  # noqa: N802
        if not self._authorized():
            return
        route = urlsplit(self.path).path.rstrip("/") or "/"
        if route in ("/ahk/cmd", "/ahk/decision"):
            self._ahk_post(route)
            return
        data = self._json_body()
        if data is None:
            return
        store = self.bridge.store
        if route == "/event":
            kind = str(data.get("kind", "")).strip()
            if not kind:
                self._send(400, {"ok": False, "error": "kind lazem ast"})
                return
            event_id = store.add_event(kind, data.get("payload") or {})
            self._send(200, {"ok": True, "id": event_id})
        elif route == "/cmd":
            kind = str(data.get("kind", "")).strip()
            if not kind:
                self._send(400, {"ok": False, "error": "kind lazem ast"})
                return
            command_id = store.add_command(kind, data.get("payload") or {})
            self._send(200, {"ok": True, "id": command_id})
        elif route == "/approvals":
            summary = str(data.get("summary", "")).strip()
            kind = str(data.get("kind", "generic")).strip()
            if not summary:
                self._send(400, {"ok": False, "error": "summary lazem ast"})
                return
            approval_id = store.create_approval(kind, summary, data.get("payload") or {})
            self._send(200, {"ok": True, "id": approval_id})
        elif route.startswith("/approvals/") and route.endswith("/decision"):
            try:
                approval_id = int(route.split("/")[2])
            except (ValueError, IndexError):
                self._send(400, {"ok": False, "error": "id namotabar"})
                return
            granted = bool(data.get("granted", False))
            changed = store.decide_approval(approval_id, granted)
            store.add_event("approval_decided", {"id": approval_id, "granted": granted})
            self._send(200, {"ok": True, "changed": changed})
        else:
            self._send(404, {"ok": False, "error": "masir nashenas"})
    def _ahk_post(self, route: str) -> None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length).decode("utf-8") if length else ""
        except (ValueError, UnicodeDecodeError):
            self._send(400, {"ok": False, "error": "badane namotabar"})
            return
        store = self.bridge.store
        if route == "/ahk/decision":
            parts = raw.split()
            if len(parts) != 2 or not parts[0].isdigit():
                self._send(400, {"ok": False, "error": "format: id granted"})
                return
            approval_id, granted = int(parts[0]), parts[1] == "1"
            changed = store.decide_approval(approval_id, granted)
            store.add_event("approval_decided", {"id": approval_id, "granted": granted})
            self._send(200, {"ok": True, "changed": changed})
            return
        parts = raw.split("\t")
        kind = parts[0].strip()
        payload: dict = {}
        if kind == "takeover" and len(parts) > 1:
            payload = {"line": parts[1]}
        elif kind == "run_mission" and len(parts) > 1:
            payload = {"path": parts[1]}
        elif kind == "learn_start" and len(parts) > 1:
            payload = {"url": parts[1]}
        elif kind == "learn_stop" and len(parts) > 1:
            payload = {"name": parts[1]}
        elif kind == "replay" and len(parts) > 1:
            values = {}
            for pair in (parts[2].split(";") if len(parts) > 2 else []):
                if "=" in pair:
                    key, value = pair.split("=", 1)
                    values[key] = value
            payload = {"name": parts[1], "values": values}
        if not kind:
            self._send(400, {"ok": False, "error": "kind lazem ast"})
            return
        self._send(200, {"ok": True, "id": store.add_command(kind, payload)})
def _ahk_clean(text) -> str:
    return " ".join(str(text).split()).replace("|", "/")[:300]


def _ahk_summary(kind: str, payload: dict) -> str:
    for key in ("text", "summary", "error", "result", "name", "status", "path", "url"):
        if payload.get(key):
            return _ahk_clean(payload[key])
    return _ahk_clean(kind)



class BridgeServer:
    def __init__(self, store: Store, host: str | None = None, port: int | None = None):
        self.store = store
        self.host = host or cfg("bridge_host")
        self.port = int(port or cfg("bridge_port"))
        self.token = ensure_token()
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._engine_flag = threading.Event()

    def set_engine_alive(self, alive: bool) -> None:
        self._engine_flag.set() if alive else self._engine_flag.clear()

    def engine_alive(self) -> bool:
        return (time.time() - float(self.store.kv_get("engine_heartbeat", 0))) < 6

    def start(self) -> None:
        handler = type("BoundHandler", (_Handler,), {"bridge": self})
        self._httpd = ThreadingHTTPServer((self.host, self.port), handler)
        self._httpd.daemon_threads = True
        self._thread = threading.Thread(target=self._httpd.serve_forever, name="bridge", daemon=True)
        self._thread.start()
        log.info("bridge listening on http://%s:%s", self.host, self.port)

    def stop(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None
        if self._thread is not None:
            self._thread.join(timeout=3)
            self._thread = None

    def serve_forever(self) -> None:
        self.start()
        try:
            while True:
                time.sleep(0.5)
        except KeyboardInterrupt:
            log.info("bridge stopped by user")
        finally:
            self.stop()


class BridgeClient:
    """Used by engine-side workers; stdlib only."""

    def __init__(self, base_url: str | None = None, token: str | None = None, timeout: float = 10.0):
        self.base_url = base_url or f"http://{cfg('bridge_host')}:{cfg('bridge_port')}"
        self.token = token or ensure_token()
        self.timeout = timeout

    def _request(self, method: str, path: str, payload: dict | None = None) -> dict:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
        request = Request(self.base_url + path, data=body, method=method)
        request.add_header(TOKEN_HEADER, self.token)
        if body is not None:
            request.add_header("Content-Type", "application/json; charset=utf-8")
        try:
            with urlopen(request, timeout=self.timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except (URLError, OSError, ValueError) as exc:
            raise RuntimeError(f"bridge dar dastres nist: {exc}") from exc

    def health(self) -> dict:
        return self._request("GET", "/health")

    def emit(self, kind: str, payload: dict | None = None) -> int:
        return int(self._request("POST", "/event", {"kind": kind, "payload": payload or {}})["id"])

    def poll_command(self) -> dict | None:
        return self._request("GET", "/cmd/next")["command"]

    def create_approval(self, kind: str, summary: str, payload: dict | None = None) -> int:
        return int(self._request(
            "POST", "/approvals", {"kind": kind, "summary": summary, "payload": payload or {}}
        )["id"])

    def approval_state(self, approval_id: int) -> str:
        return str(self._request("GET", f"/approvals/{approval_id}")["approval"]["state"])

    def request_approval(self, kind: str, summary: str, payload: dict | None = None,
                         timeout_sec: float | None = None) -> bool:
        """Blocks until the user answers in the HUD; timeout counts as denial."""
        approval_id = self.create_approval(kind, summary, payload)
        self.emit("approval_request", {"id": approval_id, "kind": kind, "summary": summary})
        deadline = time.time() + (timeout_sec if timeout_sec is not None else cfg("approval_timeout_sec"))
        while time.time() < deadline:
            state = self.approval_state(approval_id)
            if state == "granted":
                return True
            if state == "denied":
                return False
            time.sleep(0.5)
        self.emit("approval_timeout", {"id": approval_id, "summary": summary})
        return False