"""Run a command the watcher itself produced, never one supplied by the page.

The page only returns an opaque id. This module re-checks that the argv is a
python -m invocation of one of our own packages before anything is launched.
"""
from __future__ import annotations

import subprocess
import sys
import time

from agent.config import child_env, get_logger
from agent.errors import AgentError

log = get_logger("farsi.execute")

ALLOWED_PACKAGES = {"agent", "brain", "farsi"}
CREATE_NO_WINDOW = 0x08000000
DEFAULT_TIMEOUT = 120.0
MAX_DETAIL = 400


def check_argv(argv: list) -> list[str]:
    if not isinstance(argv, list) or len(argv) < 3:
        raise AgentError("دستور نامعتبر است")
    if argv[1] != "-m" or str(argv[2]) not in ALLOWED_PACKAGES:
        raise AgentError("فقط دستورهای خودِ ایجنت اجازهٔ اجرا دارند")
    return [str(part) for part in argv]


def run_argv(argv: list, *, timeout: float | None = None) -> dict:
    parts = check_argv(argv)
    parts[0] = sys.executable  # the interpreter path is ours, not the caller's
    limit = float(timeout if timeout is not None else DEFAULT_TIMEOUT)
    started = time.time()
    try:
        done = subprocess.run(
            parts, env=child_env(), stdin=subprocess.DEVNULL,
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=limit, creationflags=CREATE_NO_WINDOW,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "status": "timeout", "detail": "زمان اجرا تمام شد",
                "duration": round(time.time() - started, 2)}
    except OSError as exc:
        return {"ok": False, "status": "error", "detail": str(exc)[:MAX_DETAIL],
                "duration": round(time.time() - started, 2)}
    tail = (done.stderr or done.stdout or "").strip().splitlines()
    return {"ok": done.returncode == 0,
            "status": "ok" if done.returncode == 0 else "error",
            "detail": (tail[-1] if tail else "")[:MAX_DETAIL],
            "duration": round(time.time() - started, 2)}