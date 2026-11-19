"""Autonomous Python lessons: ask the chat worker, verify by execution, keep what runs.

The lesson text is never trusted. Every example is executed in the probe, so an
example only becomes knowledge when its own assertions pass.
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from agent.config import REPORT_DIR, STATE_DIR, assert_writable, get_logger
from agent.errors import AgentError

from . import knowledge, probe

log = get_logger("lesson")

LESSON_DIR = STATE_DIR / "lessons"
TOPICS_PATH = LESSON_DIR / "topics.json"
PROGRESS_PATH = LESSON_DIR / "progress.json"

SEED_TOPICS = [
    "انواع داده و متغیرها", "رشته‌ها و قالب‌بندی", "لیست و تاپل",
    "دیکشنری و مجموعه", "comprehension", "شرط و حلقه",
    "تابع و آرگومان‌ها", "args و kwargs و مقدار پیش‌فرض",
    "closure و دامنهٔ نام‌ها", "decorator", "کلاس و متد", "dataclass",
    "exception و try/except", "context manager و with",
    "iterator و generator", "type hint و typing",
    "pathlib و کار با فایل", "json", "csv", "عبارت باقاعده (re)",
    "datetime و time", "collections", "itertools و functools",
    "hashlib و base64", "math و statistics", "مرتب‌سازی با کلید",
    "بازگشت (recursion)", "تست با assert",
]

PROMPT = (
    "موضوع درس: {topic}\n\n"
    "یک توضیح کوتاه بده، بعد ۳ تا ۵ مثال کامل و قابل اجرا بنویس.\n"
    "قواعد مثال‌ها:\n"
    "- هر مثال مستقل باشد و فقط از کتابخانهٔ استاندارد پایتون استفاده کند.\n"
    "- هر مثال در یک بلوک کد جداگانه باشد.\n"
    "- داخل هر مثال با assert درستی خودش را بررسی کن.\n"
    "- ورودی تعاملی، شبکه، subprocess و پنجرهٔ گرافیکی نداشته باشد.\n"
    "- اگر فایل می‌نویسی، فقط نام نسبی مثل out.txt باشد.\n"
    "اگر پاسخ کامل نشد، در انتها بنویس: ادامه\n"
)


def _load(path: Path, fallback):
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            pass
    return fallback


def _save(path: Path, data) -> None:
    target = assert_writable(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def topics() -> list[str]:
    data = _load(TOPICS_PATH, None)
    if not isinstance(data, list) or not data:
        _save(TOPICS_PATH, SEED_TOPICS)
        return list(SEED_TOPICS)
    return [str(item) for item in data]


def progress() -> dict:
    data = _load(PROGRESS_PATH, {})
    return data if isinstance(data, dict) else {}


def next_topic() -> str | None:
    done = progress()
    for topic in topics():
        if topic not in done:
            return topic
    return None


def prompt_for(topic: str, digest: str = "") -> str:
    body = PROMPT.format(topic=topic)
    return (digest.strip() + "\n\n" + body) if digest.strip() else body


def absorb(db, topic: str, blocks: list[dict], *, budget=None, timeout=None) -> dict:
    """Execute each example; keep only the ones that pass."""
    stats = {"topic": topic, "blocks": 0, "verified": 0, "rejected": 0, "units": 0}
    for index, block in enumerate(blocks, start=1):
        code = str(block.get("code") or "")
        if not code.strip():
            continue
        try:
            report = probe.run_snippet(code, timeout=timeout, budget=budget, label="lesson")
        except AgentError:
            break
        stats["blocks"] += 1
        if report["status"] != "ok":
            stats["rejected"] += 1
            continue
        stats["verified"] += 1
        origin = f"lesson:{topic}#{index}"
        for signature in knowledge.learn_source(db, "python", origin, code):
            knowledge.record_outcome(db, signature, "ok", "lesson probe", report["duration"])
            stats["units"] += 1
    return stats


def run_lesson(session, db, topic: str, *, parts: int = 2,
               budget=None, timeout=None) -> dict:
    from . import capability, digest

    caps = capability.prepare()
    try:
        summary = digest.render(digest.build(caps, db, topics=topics(), done=progress()))
    finally:
        caps.close()
    report = session.run_task(prompt_for(topic, summary), write=False, max_parts=parts)
    stats = absorb(db, topic, report.get("blocks") or [], budget=budget, timeout=timeout)
    stats["parts"] = len(report.get("parts") or [])
    stats["ts"] = time.time()
    done = progress()
    done[topic] = stats
    _save(PROGRESS_PATH, done)
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    target = assert_writable(REPORT_DIR / f"lesson_{int(stats['ts'])}.json")
    target.write_text(json.dumps(stats, ensure_ascii=False, indent=2), encoding="utf-8")
    return stats


def _open(url: str | None):
    from agent.ai import ChatSession, _chat_url
    from agent.browser import Browser
    from agent.notify import Notifier
    from agent.store import Store

    store = Store()
    notifier = Notifier(interactive=True)
    browser = Browser(store, notifier)
    browser.start()
    session = ChatSession(browser, store, notifier)
    session.open(_chat_url(url))
    return store, browser, session


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="brain.lesson", description="درس‌گیر خودکار پایتون")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("plan", help="برنامهٔ درسی و پیشرفت")
    ask = sub.add_parser("ask", help="یک موضوع مشخص")
    ask.add_argument("--topic", required=True)
    ask.add_argument("--parts", type=int, default=2)
    ask.add_argument("--url", default=None)
    ask.add_argument("--dry-run", action="store_true")
    nxt = sub.add_parser("next", help="موضوع بعدیِ نخوانده")
    nxt.add_argument("--parts", type=int, default=2)
    nxt.add_argument("--url", default=None)
    nxt.add_argument("--dry-run", action="store_true")
    auto = sub.add_parser("auto", help="چند درس پشت سر هم")
    auto.add_argument("--count", type=int, default=3)
    auto.add_argument("--parts", type=int, default=2)
    auto.add_argument("--url", default=None)
    sub.add_parser("stats", help="آمار درس‌ها")
    sub.add_parser("reset", help="پاک کردن پیشرفت (دانش می‌ماند)")
    args = parser.parse_args(argv)

    if args.command == "plan":
        done = progress()
        for index, topic in enumerate(topics(), start=1):
            mark = "✓" if topic in done else "·"
            info = done.get(topic, {})
            extra = f"  ({info.get('verified', 0)} مثال تأییدشده)" if info else ""
            print(f"{mark} {index:2}. {topic}{extra}")
        return 0
    if args.command == "stats":
        done = progress()
        verified = sum(item.get("verified", 0) for item in done.values())
        rejected = sum(item.get("rejected", 0) for item in done.values())
        print(f"درس‌های خوانده‌شده: {len(done)} از {len(topics())}")
        print(f"مثال تأییدشده: {verified} | ردشده: {rejected}")
        return 0
    if args.command == "reset":
        _save(PROGRESS_PATH, {})
        print("پیشرفت پاک شد؛ دانش ذخیره‌شده دست‌نخورده ماند.")
        return 0

    wanted = [args.topic] if args.command == "ask" else []
    if args.command in ("next", "auto"):
        count = 1 if args.command == "next" else max(1, args.count)
        done = progress()
        for topic in topics():
            if topic not in done and len(wanted) < count:
                wanted.append(topic)
        if not wanted:
            print("همهٔ موضوع‌ها خوانده شده‌اند. با reset می‌توانی دوباره شروع کنی.")
            return 0
    if getattr(args, "dry_run", False):
        for topic in wanted:
            print("--- درخواست برای:", topic, "---")
            print(prompt_for(topic))
        return 0

    db = knowledge.connect()
    store = browser = None
    budget = probe.Budget(max_runs=len(wanted) * 8 + 4, max_seconds=240.0)
    try:
        store, browser, session = _open(args.url)
        for topic in wanted:
            print("درس:", topic)
            stats = run_lesson(session, db, topic, parts=args.parts, budget=budget)
            print(f"  مثال: {stats['blocks']} | تأیید: {stats['verified']} "
                  f"| رد: {stats['rejected']} | الگو: {stats['units']}")
            if budget.runs >= budget.max_runs or budget.seconds >= budget.max_seconds:
                print("سهمیهٔ نشست تمام شد؛ بقیه در نشست بعد.")
                break
        return 0
    except (AgentError, OSError, ValueError) as exc:
        print("توقف:", exc)
        return 1
    finally:
        if browser is not None:
            browser.close()
        if store is not None:
            store.close()
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())