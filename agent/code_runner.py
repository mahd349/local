"""Run generated code inside the sandbox, detect success, repair via the chat worker.

No danger-scoring blacklist: the guards are the path jail, a timeout, the
kill-switch, and one approval per new/changed file.
"""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import subprocess
import sys
import time
from ctypes import wintypes
from pathlib import Path

from .config import (
    REPORT_DIR, STATE_DIR, assert_writable, cfg, child_env, get_logger,
)
from .errors import AgentError
from .notify import Notifier
from .store import Store

log = get_logger("code_runner")
LOG_DIR = STATE_DIR / "runlogs"


def is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except OSError:
        return False


def has_window(pid: int) -> bool:
    user32 = ctypes.windll.user32
    enum_proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.IsWindowVisible.restype = wintypes.BOOL
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.EnumWindows.argtypes = [enum_proc, wintypes.LPARAM]
    user32.EnumWindows.restype = wintypes.BOOL
    found: list[int] = []

    @enum_proc
    def callback(hwnd, lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        owner = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value == pid:
            found.append(int(hwnd))
            return False
        return True

    user32.EnumWindows(callback, 0)
    return bool(found)


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _approved(store: Store, notifier: Notifier, path: Path) -> bool:
    key = "run_approved:" + str(path)
    digest = _digest(path)
    if store.kv_get(key) == digest:
        return True
    summary = f"اجرای این فایل اجازه داده می‌شود؟\n{path}"
    if is_admin():
        summary += "\nهشدار: کنسول با دسترسی ادمین اجرا شده است."
    if not notifier.approval("run_code", summary, {"path": str(path)}):
        return False
    store.kv_set(key, digest)
    return True


def run_python(path: str | Path, store: Store, notifier: Notifier,
               timeout: float | None = None, grace: float | None = None,
               approve: bool = True) -> dict:
    target = assert_writable(path)
    if not target.exists():
        raise AgentError(f"فایل پیدا نشد: {target}")
    if approve and not _approved(store, notifier, target):
        return {"status": "denied", "exit_code": None, "stdout": "", "stderr": "",
                "duration": 0.0, "log": ""}

    limit = float(timeout if timeout is not None else cfg("run_timeout_sec"))
    window_grace = min(float(grace if grace is not None else cfg("gui_grace_sec")), limit)
    started = time.time()
    process = subprocess.Popen(
        [sys.executable, str(target)], cwd=str(target.parent), env=child_env(),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", errors="replace",
    )
    try:
        out, err = process.communicate(timeout=window_grace)
        status = "ok" if process.returncode == 0 else "error"
    except subprocess.TimeoutExpired:
        # Slow machines may show the window a bit after the grace boundary.
        probe_deadline = time.time() + 5
        while time.time() < probe_deadline and not has_window(process.pid):
            time.sleep(0.5)
        if has_window(process.pid):
            status = "gui_ok"
            process.kill()
            out, err = process.communicate()
        else:
            try:
                out, err = process.communicate(timeout=max(0.1, limit - window_grace))
                status = "ok" if process.returncode == 0 else "error"
            except subprocess.TimeoutExpired:
                status = "timeout"
                process.kill()
                out, err = process.communicate()

    duration = time.time() - started
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = assert_writable(LOG_DIR / f"{target.stem}_{int(started)}.log")
    log_path.write_text((out or "") + "\n--- stderr ---\n" + (err or ""), encoding="utf-8")
    notifier.emit("code_run", {"path": str(target), "status": status,
                               "duration": round(duration, 2)})
    return {"status": status, "exit_code": process.returncode, "stdout": out or "",
            "stderr": err or "", "duration": round(duration, 2), "log": str(log_path)}


def _fix_prompt(target: Path, result: dict) -> str:
    source = target.read_text(encoding="utf-8", errors="replace")[:12000]
    error = (result["stderr"] or result["stdout"] or "")[-4000:]
    return (
        "این فایل پایتون خطا داد. نسخهٔ کامل و اصلاح‌شدهٔ همان فایل را برگردان.\n"
        f"### FILE: {target.name}\n"
        "قرارداد: کل فایل را در یک بلوک کد بده و در انتها دقیقاً بنویس: اتمام کد\n\n"
        f"--- کد فعلی ---\n{source}\n\n--- خطا ({result['status']}) ---\n{error}\n"
    )


def fix_loop(path: str | Path, session, store: Store, notifier: Notifier,
             max_loops: int | None = None) -> dict:
    target = assert_writable(path)
    loops = int(max_loops if max_loops is not None else cfg("fix_loops"))
    attempts: list[dict] = []
    status = "failed"
    for index in range(loops + 1):
        notifier.check_stop()
        result = run_python(target, store, notifier)
        attempts.append(result)
        if result["status"] in ("ok", "gui_ok"):
            status = "ok"
            break
        if result["status"] == "denied":
            status = "denied"
            break
        if index >= loops:
            break
        notifier.emit("fix_request", {"path": str(target), "loop": index + 1})
        report = session.run_task(_fix_prompt(target, result), out_dir=str(target.parent))
        if not report.get("written"):
            notifier.emit("fix_no_code", {"path": str(target)})
            break

    payload = {"status": status, "path": str(target), "attempts": attempts,
               "loops_used": max(0, len(attempts) - 1)}
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    report_path = assert_writable(REPORT_DIR / f"fix_{target.stem}_{int(time.time())}.json")
    report_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    payload["report"] = str(report_path)
    notifier.emit("fix_done", {"path": str(target), "status": status})
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agent.code_runner", description="اجرا و تعمیر کد")
    sub = parser.add_subparsers(dest="command", required=True)
    run_parser = sub.add_parser("run", help="اجرای یک فایل در سندباکس")
    run_parser.add_argument("path")
    run_parser.add_argument("--timeout", type=float, default=None)
    fix_parser = sub.add_parser("fix", help="اجرا + فرستادن خطا به AI + تعمیر")
    fix_parser.add_argument("path")
    fix_parser.add_argument("--url", default=None)
    fix_parser.add_argument("--loops", type=int, default=None)
    args = parser.parse_args(argv)

    store = Store()
    notifier = Notifier(interactive=True)
    try:
        if args.command == "run":
            result = run_python(args.path, store, notifier, timeout=args.timeout)
            print("وضعیت:", result["status"], "| کد خروج:", result["exit_code"])
            print("لاگ:", result["log"])
            if result["stderr"]:
                print(result["stderr"][-1500:])
            return 0 if result["status"] in ("ok", "gui_ok") else 1

        from .ai import ChatSession, _chat_url, _protocol
        from .browser import Browser
        with Browser(store, notifier) as browser:
            session = ChatSession(browser, store, notifier)
            session.open(_chat_url(args.url))
            session.run_task(_protocol(None) + "\n\nآماده‌ای؟ فقط بنویس: اتمام کد",
                             write=False, max_parts=1)
            report = fix_loop(args.path, session, store, notifier, max_loops=args.loops)
        print("وضعیت:", report["status"], "| حلقه‌ها:", report["loops_used"])
        print("گزارش:", report["report"])
        return 0 if report["status"] == "ok" else 1
    except (AgentError, KeyboardInterrupt) as exc:
        print("توقف:", exc)
        return 1
    finally:
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())