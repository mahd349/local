"""Command line entry for the local agent."""

from __future__ import annotations

import argparse
import importlib
import socket
import sys
import time
from pathlib import Path

from playwright.sync_api import Error as PlaywrightError

from . import guard, installer, learntest, missiontest, selftest
from .browser import Browser
from .bridge import BridgeServer, ensure_token
from .config import (
    DB_PATH, DOWNLOAD_DIR, LIB_DIR, PROFILE_DIR, STATE_DIR, VERSION,
    WRITE_ROOTS, cfg, ensure_dirs, get_logger, load_config,
)
from .dsl import load_mission, next_due
from .errors import AgentError
from .learn import Recorder, replay as replay_macro, save_macro
from .netcap import NetworkCapture
from .notify import Notifier
from .runner import Runner
from .store import Store
from .takeover import execute as takeover_execute

log = get_logger("cli")

_BROWSER_CANDIDATES = {
    "msedge": (
        Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
        Path(r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"),
    ),
    "chrome": (
        Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
        Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
    ),
}


def _doctor() -> int:
    ensure_dirs()
    problems = 0
    print(f"Agent {VERSION} — بررسی محیط")
    print(f"Python: {sys.version.split()[0]} ({sys.executable})")
    if sys.version_info < (3, 11):
        print("  ! نسخه پایتون باید ۳.۱۱ یا بالاتر باشد.")
        problems += 1
    for directory in (LIB_DIR, DOWNLOAD_DIR, PROFILE_DIR, STATE_DIR):
        try:
            probe = directory / ".write_test"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
            print(f"  ✓ {directory}")
        except OSError as exc:
            print(f"  ! نوشتن در {directory} ممکن نیست: {exc}")
            problems += 1
    print("مسیرهای مجاز نوشتن:", "، ".join(str(p) for p in WRITE_ROOTS))
    for pkg in installer.PACKAGES:
        try:
            module = importlib.import_module(pkg.module)
            print(f"  ✓ {pkg.name} {getattr(module, '__version__', '')}")
        except ImportError:
            print(f"  ! {pkg.name} نصب نیست → دستور: python -m agent deps")
            problems += 1
    channel = cfg("browser_channel")
    found = next((p for p in _BROWSER_CANDIDATES.get(channel, ()) if p.exists()), None)
    if found:
        print(f"  ✓ مرورگر {channel}: {found}")
    else:
        print(f"  ! مرورگر {channel} پیدا نشد.")
        problems += 1
    host, port = cfg("bridge_host"), int(cfg("bridge_port"))
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        busy = sock.connect_ex((host, port)) == 0
    print(f"  {'✓' if not busy else '!'} پورت {host}:{port} {'اشغال است (پل بالاست؟)' if busy else 'آزاد است'}")
    print(f"توکن پل: {ensure_token()[:6]}… ({DB_PATH.parent / 'bridge.token'})")
    print(f"دیتابیس: {DB_PATH}")
    print(f"لینک چت پیش‌فرض: {cfg('chat_url')}")
    print("نتیجه:", "سالم" if problems == 0 else f"{problems} مورد نیاز به رسیدگی دارد")
    return 0 if problems == 0 else 1


def _bridge(host: str | None, port: int | None) -> int:
    ensure_dirs()
    load_config()
    store = Store()
    for old in store.pending_approvals():
        store.decide_approval(old["id"], False)
    server = BridgeServer(store, host=host, port=port)
    print(f"پل روی http://{server.host}:{server.port} بالا آمد. توقف: Ctrl+C")
    try:
        server.serve_forever()
    finally:
        store.close()
    return 0


def _deps(assume_yes: bool, all_packages: bool) -> int:
    targets = list(installer.PACKAGES) if all_packages else installer.missing_packages()
    return installer.install(targets, assume_yes=assume_yes)


def _browse(url: str, seconds: int) -> int:
    store = Store()
    notifier = Notifier(interactive=True)
    capture = None
    try:
        with Browser(store, notifier) as browser:
            page = browser.new_page()
            if cfg("netcap_enabled"):
                capture = NetworkCapture(store, notifier)
                capture.attach(page)
            page.goto(url, wait_until="domcontentloaded")
            page.bring_to_front()
            guard.ensure_clear(page, notifier, wait=True, screenshot=browser.screenshot)
            print("مرورگر باز است. تب فعال:", browser.active_page().url)
            print(f"تا {seconds} ثانیه باز می‌ماند؛ Ctrl+C برای بستن.")
            deadline = time.time() + seconds
            while time.time() < deadline:
                notifier.check_stop()
                browser.flush_downloads()
                page.wait_for_timeout(500)
            if capture is not None:
                print(f"درخواست‌های ثبت‌شده: {len(capture.entries)}")
    except KeyboardInterrupt:
        print("بسته شد.")
    except AgentError as exc:
        print("توقف:", exc)
        return 1
    finally:
        if capture is not None:
            capture.detach()
        store.close()
    return 0


def _mission_paths(paths: list[str]) -> list[Path]:
    found: list[Path] = []
    for item in paths:
        path = Path(item)
        if path.is_dir():
            found.extend(sorted(p for p in path.iterdir() if p.suffix.lower() in (".yaml", ".yml")))
        else:
            found.append(path)
    if not found:
        raise AgentError("هیچ فایل مأموریتی پیدا نشد")
    return found


def _run(paths: list[str]) -> int:
    store = Store()
    notifier = Notifier(interactive=True)
    try:
        missions = [load_mission(p) for p in _mission_paths(paths)]
        reports = Runner(store, notifier).run_queue(missions)
        failed = 0
        for report in reports:
            print(f"{report['mission']}: {report['status']} → {report['report_path']}")
            if report["status"] != "ok":
                failed += 1
                print("  خطا:", report["error"])
        return 1 if failed else 0
    except AgentError as exc:
        print("توقف:", exc)
        return 1
    finally:
        store.close()


def _watch(paths: list[str]) -> int:
    store = Store()
    notifier = Notifier(interactive=True)
    try:
        scheduled = [m for m in (load_mission(p) for p in _mission_paths(paths)) if m.schedule]
        if not scheduled:
            print("هیچ مأموریت زمان‌بندی‌شده‌ای نیست (فیلد schedule لازم است).")
            return 1
        print("زمان‌بندی فعال است؛ Ctrl+C برای توقف.")
        while True:
            now = time.time()
            for mission in scheduled:
                key = "schedule:" + mission.source
                due = next_due(mission.schedule, store.kv_get(key), now)
                if due is not None and now >= due:
                    store.kv_set(key, now)
                    Runner(store, notifier).run_mission(mission)
            notifier.check_stop()
            time.sleep(5)
    except KeyboardInterrupt:
        print("زمان‌بندی متوقف شد.")
        return 0
    except AgentError as exc:
        print("توقف:", exc)
        return 1
    finally:
        store.close()


def _learn(url: str, name: str | None) -> int:
    import msvcrt

    store = Store()
    notifier = Notifier(interactive=True)
    try:
        with Browser(store, notifier) as browser:
            page = browser.new_page(url)
            recorder = Recorder(store, notifier)
            recorder.attach(page)
            browser.context.on("page", recorder.attach)
            print("در حال ضبط. در مرورگر کار کن؛ برای پایان هر کلیدی را بزن.")
            while not msvcrt.kbhit():
                recorder.drain(browser.pages())
                browser.flush_downloads()
                page.wait_for_timeout(400)
            msvcrt.getch()
            recorder.drain(browser.pages())
            print(f"{len(recorder.events)} رویداد ضبط شد.")
            if not recorder.events:
                print("چیزی ضبط نشد.")
                return 1
            macro_name = name or input("نام ماکرو: ").strip()
            if not macro_name:
                print("نام لازم است.")
                return 1
            macro = save_macro(store, macro_name, recorder.events)
            print("پارامترها:", ", ".join(
                p["name"] + (" (محرمانه)" if p["secret"] else "") for p in macro["params"]))
            print(f"ماکرو ذخیره شد: {macro_name}")
        return 0
    except KeyboardInterrupt:
        print("ضبط لغو شد.")
        return 1
    except AgentError as exc:
        print("توقف:", exc)
        return 1
    finally:
        store.close()


def _macros() -> int:
    store = Store()
    try:
        names = store.list_macros()
        if not names:
            print("هیچ ماکرویی ذخیره نشده.")
            return 0
        for name in names:
            macro = store.get_macro(name)
            params = ", ".join(p["name"] for p in macro["params"]) or "بدون پارامتر"
            print(f"{name} — {len(macro['steps'])} مرحله — پارامترها: {params}")
        return 0
    finally:
        store.close()


def _replay(name: str, sets: list[str]) -> int:
    store = Store()
    notifier = Notifier(interactive=True)
    try:
        values = {}
        for item in sets:
            if "=" not in item:
                raise AgentError(f"--set باید key=value باشد: {item}")
            key, value = item.split("=", 1)
            values[key] = value
        with Browser(store, notifier) as browser:
            report = replay_macro(store, notifier, browser, name, values)
            print(f"{name}: {report['status']} → {report['report_path']}")
            if report["status"] != "ok":
                print("  خطا:", report["error"])
            return 0 if report["status"] == "ok" else 1
    except AgentError as exc:
        print("توقف:", exc)
        return 1
    finally:
        store.close()


def _takeover(url: str | None, command: str | None) -> int:
    store = Store()
    notifier = Notifier(interactive=True)
    try:
        with Browser(store, notifier) as browser:
            if url:
                browser.new_page(url)
            if command:
                print(takeover_execute(browser, store, notifier, command))
                return 0
            print("Takeover فعال است. help برای راهنما، exit برای خروج.")
            while True:
                try:
                    line = input("agent> ")
                except EOFError:
                    break
                if line.strip().lower() in ("exit", "quit"):
                    break
                try:
                    result = takeover_execute(browser, store, notifier, line)
                    if result:
                        print(result)
                except (AgentError, PlaywrightError) as exc:
                    print("خطا:", exc)
        return 0
    except KeyboardInterrupt:
        print("بسته شد.")
        return 0
    except AgentError as exc:
        print("توقف:", exc)
        return 1
    finally:
        store.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agent", description="ایجنت محلی مرورگر")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("doctor", help="بررسی محیط")
    bridge_parser = sub.add_parser("bridge", help="اجرای پل محلی برای AHK")
    bridge_parser.add_argument("--host", default=None)
    bridge_parser.add_argument("--port", type=int, default=None)
    deps_parser = sub.add_parser("deps", help="اعلام و نصب کتابخانه‌ها روی D:")
    deps_parser.add_argument("--yes", action="store_true")
    deps_parser.add_argument("--all", action="store_true")
    browse_parser = sub.add_parser("browse", help="باز کردن مرورگر ایجنت")
    browse_parser.add_argument("--url", default="https://example.com")
    browse_parser.add_argument("--seconds", type=int, default=120)
    sub.add_parser("selftest", help="تست آفلاین موتور مرورگر")
    sub.add_parser("missiontest", help="تست آفلاین Mission Mode")
    sub.add_parser("learntest", help="تست آفلاین Learn و Takeover")
    run_parser = sub.add_parser("run", help="اجرای مأموریت‌های YAML")
    run_parser.add_argument("paths", nargs="+")
    watch_parser = sub.add_parser("watch", help="اجرای زمان‌بندی‌شده")
    watch_parser.add_argument("paths", nargs="+")
    learn_parser = sub.add_parser("learn", help="ضبط حرکات من و ساخت ماکرو")
    learn_parser.add_argument("--url", default="https://example.com")
    learn_parser.add_argument("--name", default=None)
    sub.add_parser("macros", help="فهرست ماکروهای ذخیره‌شده")
    replay_parser = sub.add_parser("replay", help="بازپخش یک ماکرو")
    replay_parser.add_argument("name")
    replay_parser.add_argument("--set", action="append", default=[], dest="sets")
    take_parser = sub.add_parser("takeover", help="کنترل تب فعال با زبان دستور")
    take_parser.add_argument("--url", default=None)
    take_parser.add_argument("--command", default=None)
    ai_parser = sub.add_parser("ai", help="کارگر چت مرورگری (پرسش از Qwen/DeepSeek)")
    ai_parser.add_argument("ai_args", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)

    if args.command == "doctor":
        return _doctor()
    if args.command == "bridge":
        return _bridge(args.host, args.port)
    if args.command == "browse":
        return _browse(args.url, args.seconds)
    if args.command == "selftest":
        return selftest.run()
    if args.command == "missiontest":
        return missiontest.run()
    if args.command == "learntest":
        return learntest.run()
    if args.command == "run":
        return _run(args.paths)
    if args.command == "watch":
        return _watch(args.paths)
    if args.command == "learn":
        return _learn(args.url, args.name)
    if args.command == "macros":
        return _macros()
    if args.command == "replay":
        return _replay(args.name, args.sets)
    if args.command == "takeover":
        return _takeover(args.url, args.command)
    if args.command == "ai":
        from .ai import main as ai_main
        return ai_main(args.ai_args)
    return _deps(args.yes, args.all)