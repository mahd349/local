"""Run a short snippet in a separate interpreter with a session budget.

Guards: no network, no new processes, writes only inside the run's own scratch
folder, plus a wall-clock limit. This is a practical guard for generated code,
not an operating-system level isolation.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

from agent.config import STATE_DIR, assert_writable, child_env, get_logger
from agent.errors import AgentError

log = get_logger("probe")

PROBE_ROOT = STATE_DIR / "probes"
RUNNER = Path(__file__).with_name("_probe_runner.py")
DEFAULT_TIMEOUT = 5.0
CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

DENY_IMPORTS = (
    "socket", "_socket", "ssl", "_ssl", "subprocess", "ctypes", "_ctypes",
    "multiprocessing", "_multiprocessing", "concurrent", "asyncio",
    "http", "urllib.request", "urllib.error", "urllib.robotparser",
    "ftplib", "smtplib", "telnetlib", "poplib", "imaplib", "nntplib", "xmlrpc",
    "webbrowser", "pickle", "shelve", "marshal", "dbm", "sqlite3", "_sqlite3",
    "winreg", "msvcrt", "signal", "pty", "tty", "curses",
    "tkinter", "turtle", "turtledemo", "idlelib",
    "unittest", "doctest", "pdb", "bdb",
    "ensurepip", "venv", "pip", "setuptools", "distutils",
)

DENY_EVENTS = (
    "socket.__new__", "socket.connect", "socket.bind", "socket.getaddrinfo",
    "socket.gethostbyname", "socket.sendto", "socket.sendmsg",
    "subprocess.Popen", "os.system", "os.exec", "os.posix_spawn", "os.spawn",
    "os.fork", "os.forkpty", "os.kill", "os.startfile",
    "ctypes.dlopen", "ctypes.dlsym", "ctypes.call_function",
    "winreg.OpenKey", "winreg.CreateKey", "winreg.SetValue", "winreg.DeleteKey",
    "builtins.input", "webbrowser.open",
)


class Budget:
    """Caps how much work one learning session may do."""

    def __init__(self, max_runs: int = 40, max_seconds: float = 90.0):
        self.max_runs = int(max_runs)
        self.max_seconds = float(max_seconds)
        self.runs = 0
        self.seconds = 0.0

    def check(self) -> None:
        if self.runs >= self.max_runs:
            raise AgentError("سهمیهٔ تعداد اجرا تمام شد")
        if self.seconds >= self.max_seconds:
            raise AgentError("سهمیهٔ زمان اجرا تمام شد")

    def consume(self, duration: float) -> None:
        self.runs += 1
        self.seconds += float(duration)

    def as_dict(self) -> dict:
        return {"runs": self.runs, "seconds": round(self.seconds, 2),
                "max_runs": self.max_runs, "max_seconds": self.max_seconds}


def run_snippet(source: str, *, timeout: float | None = None,
                budget: Budget | None = None, label: str = "probe",
                keep: bool = False, allow_write: tuple | list = ()) -> dict:
    if not RUNNER.exists():
        raise AgentError(f"فایل اجراکننده پیدا نشد: {RUNNER}")
    if budget is not None:
        budget.check()

    limit = float(timeout if timeout is not None else DEFAULT_TIMEOUT)
    name = f"{label}_{int(time.time())}_{uuid.uuid4().hex[:6]}"
    scratch = assert_writable(PROBE_ROOT / name)
    scratch.mkdir(parents=True, exist_ok=True)

    snippet = scratch / "snippet.py"
    result = scratch / "result.json"
    config = scratch / "config.json"
    snippet.write_text(source, encoding="utf-8")
    config.write_text(json.dumps({
        "deny_imports": list(DENY_IMPORTS),
        "deny_events": list(DENY_EVENTS),
        "recursion_limit": 400,
        "write_roots": [str(assert_writable(p)) for p in allow_write],
    }), encoding="utf-8")

    env = child_env({"TMP": str(scratch), "TEMP": str(scratch), "TMPDIR": str(scratch)})
    command = [sys.executable, "-I", "-S", "-B", "-X", "utf8", str(RUNNER),
               str(snippet), str(scratch), str(result), str(config)]

    started = time.time()
    timed_out = False
    raw_err = ""
    try:
        completed = subprocess.run(
            command, cwd=str(scratch), env=env, capture_output=True,
            text=True, encoding="utf-8", errors="replace",
            timeout=limit + 2.0, creationflags=CREATE_NO_WINDOW,
        )
        raw_err = completed.stderr or ""
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        raw_err = exc.stderr if isinstance(exc.stderr, str) else ""
    except OSError as exc:
        shutil.rmtree(scratch, ignore_errors=True)
        raise AgentError(f"اجرای قطعه ممکن نشد: {exc}") from exc

    duration = time.time() - started
    if budget is not None:
        budget.consume(duration)

    payload: dict = {}
    if result.exists():
        try:
            payload = json.loads(result.read_text(encoding="utf-8"))
        except ValueError:
            payload = {}

    if timed_out:
        status = "timeout"
    elif payload:
        status = str(payload.get("status", "error"))
    else:
        status = "crash"

    report = {
        "status": status,
        "detail": str(payload.get("detail", ""))[:400],
        "stdout": str(payload.get("stdout", ""))[:8000],
        "stderr": (str(payload.get("stderr", "")) or raw_err)[:8000],
        "blocked": list(payload.get("blocked", [])),
        "duration": round(duration, 3),
        "scratch": str(scratch),
    }
    if not keep:
        shutil.rmtree(scratch, ignore_errors=True)
        report["scratch"] = ""
    return report
