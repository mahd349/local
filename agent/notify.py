"""User-facing notifications, approvals and the emergency stop flag.

Works with the AHK HUD through the bridge, and degrades to the console
when the bridge is not running (standalone CLI usage).
"""

from __future__ import annotations

from typing import Any

from .bridge import BridgeClient
from .config import STATE_DIR, cfg, get_logger
from .errors import UserAbort

log = get_logger("notify")
STOP_PATH = STATE_DIR / "STOP"


class Notifier:
    def __init__(self, interactive: bool = False):
        self.interactive = interactive
        self._client: BridgeClient | None = None
        try:
            self._client = BridgeClient()
            self._client.health()
        except (RuntimeError, OSError):
            self._client = None
            log.info("bridge down; notifications go to the console")

    @property
    def online(self) -> bool:
        return self._client is not None

    def emit(self, kind: str, payload: dict[str, Any] | None = None) -> None:
        payload = payload or {}
        if self._client is not None:
            try:
                self._client.emit(kind, payload)
                return
            except RuntimeError:
                self._client = None
        log.info("[%s] %s", kind, payload)

    def approval(self, kind: str, summary: str, payload: dict | None = None,
                 timeout_sec: float | None = None) -> bool:
        self.check_stop()
        if self._client is not None:
            return self._client.request_approval(kind, summary, payload, timeout_sec)
        if not self.interactive:
            log.warning("approval denied (no HUD, non-interactive): %s", summary)
            return False
        print("\nدرخواست تأیید:", summary)
        answer = input("اجازه می‌دهی؟ [y/N] ").strip().lower()
        return answer in ("y", "yes", "بله", "b")

    def check_stop(self) -> None:
        if STOP_PATH.exists():
            raise UserAbort("توقف اضطراری فعال است (فایل STOP)")

    @staticmethod
    def clear_stop() -> None:
        if STOP_PATH.exists():
            STOP_PATH.unlink()

    def wait_hint(self, seconds: float) -> float:
        return float(cfg("guard_poll_sec") if seconds <= 0 else seconds)