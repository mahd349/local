"""Feed the knowledge store: bulk scan of Python files plus an AI inbox.

Scanning only reads structure; it never runs your files. Optional smoke
verification runs in the probe sandbox and only promotes a pattern when the
outcome is meaningful, so a unit that needs its module context is not
penalised for failing to stand alone.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import time
from pathlib import Path

from agent.codeblocks import from_markdown
from agent.config import (
    CODE_ROOT, SKIP_DIR_NAMES, assert_writable, ensure_dirs, get_logger, is_indexable,
)
from agent.errors import AgentError

from . import knowledge, probe

log = get_logger("ingest")

INBOX = CODE_ROOT / "ai_inbox"
MAX_FILE_BYTES = 400_000
TEXT_EXT = {".txt", ".md", ".markdown"}
SKIP_TAGS = {"gui", "http", "process", "async", "threading"}


def iter_python_files(roots):
    for root in roots:
        base = Path(root)
        if not base.exists() or not is_indexable(base):
            continue
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [d for d in dirnames
                           if d.lower() not in SKIP_DIR_NAMES
                           and is_indexable(Path(dirpath) / d)]
            for name in filenames:
                if name.lower().endswith(".py"):
                    yield Path(dirpath) / name


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8-sig", errors="replace")


def learn_file(db, path: Path) -> int:
    """Number of learned units, or -1 when the file was skipped."""
    try:
        if path.stat().st_size > MAX_FILE_BYTES:
            return -1
        source = _read(path)
    except OSError:
        return -1
    return len(knowledge.learn_source(db, "python", str(path), source))


def _candidates(db, limit: int) -> list[dict]:
    rows = db.execute(
        "SELECT * FROM units WHERE kind='function' AND verified=0 "
        "ORDER BY updated_ts DESC LIMIT ?", (max(1, limit) * 4,),
    ).fetchall()
    out: list[dict] = []
    for row in rows:
        item = dict(row)
        for key in ("apis", "tags", "flags", "slots"):
            item[key] = json.loads(item[key] or "[]")
        if item["flags"] or SKIP_TAGS & set(item["tags"]):
            continue
        out.append(item)
        if len(out) >= limit:
            break
    return out


def _smoke(db, units: list[dict], stats: dict) -> None:
    budget = probe.Budget(max_runs=len(units) + 1, max_seconds=60.0)
    for unit in units:
        try:
            report = probe.run_snippet(unit["source"], timeout=4.0,
                                       budget=budget, label="ingest")
        except AgentError:
            break
        stats["checked"] += 1
        if report["status"] == "ok":
            knowledge.record_outcome(db, unit["signature"], "ok",
                                     "smoke:定义 بدون خطا", report["duration"])
            stats["promoted"] += 1


def scan(roots, limit: int | None = None, verify_count: int = 0,
         db_path=None) -> dict:
    ensure_dirs()
    db = knowledge.connect(db_path)
    stats = {"files": 0, "learned": 0, "skipped": 0, "checked": 0, "promoted": 0}
    try:
        for path in iter_python_files(roots):
            count = learn_file(db, path)
            stats["files"] += 1
            if count < 0:
                stats["skipped"] += 1
            else:
                stats["learned"] += count
            if stats["files"] % 200 == 0:
                print(f"  … {stats['files']} فایل بررسی شد")
            if limit and stats["files"] >= limit:
                break
        if verify_count > 0:
            _smoke(db, _candidates(db, int(verify_count)), stats)
    finally:
        db.close()
    return stats


def _json_texts(data) -> list[str]:
    """Collect chat text from any export shape, without assuming a schema."""
    out: list[str] = []

    def walk(node):
        if isinstance(node, dict):
            for key, value in node.items():
                if key in ("content", "text") and isinstance(value, str):
                    out.append(value)
                else:
                    walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(data)
    return out


def _units_by_signature(db, signatures, limit: int) -> list[dict]:
    out: list[dict] = []
    for signature in dict.fromkeys(signatures):
        row = db.execute("SELECT * FROM units WHERE signature=?", (signature,)).fetchone()
        if row is None:
            continue
        item = dict(row)
        for key in ("apis", "tags", "flags", "slots"):
            item[key] = json.loads(item[key] or "[]")
        if item["kind"] != "function" or item["flags"]:
            continue
        if SKIP_TAGS & set(item["tags"]):
            continue
        out.append(item)
        if len(out) >= limit:
            break
    return out


def inbox(folder=None, db_path=None, verify_count: int = 0) -> dict:
    ensure_dirs()
    source_dir = Path(folder) if folder else INBOX
    source_dir.mkdir(parents=True, exist_ok=True)
    done = assert_writable(source_dir / "_done")
    done.mkdir(parents=True, exist_ok=True)
    allowed = TEXT_EXT | {".py", ".json"}
    db = knowledge.connect(db_path)
    stats = {"files": 0, "blocks": 0, "learned": 0, "ignored": 0,
             "checked": 0, "promoted": 0}
    fresh: list[str] = []
    try:
        for path in sorted(source_dir.iterdir()):
            if not path.is_file() or path.suffix.lower() not in allowed:
                if path.is_file():
                    stats["ignored"] += 1
                continue
            try:
                raw = _read(path)
            except OSError:
                stats["ignored"] += 1
                continue
            suffix = path.suffix.lower()
            if suffix == ".py":
                sources = [raw]
            elif suffix == ".json":
                try:
                    texts = _json_texts(json.loads(raw))
                except ValueError:
                    texts = [raw]
                sources = [block["code"] for text in texts
                           for block in from_markdown(text)]
            else:
                sources = [block["code"] for block in from_markdown(raw)]
                if not sources:
                    sources = [raw]
            stats["files"] += 1
            stats["blocks"] += len(sources)
            for index, code in enumerate(sources, start=1):
                signatures = knowledge.learn_source(
                    db, "python", str(path.resolve()), code)
                stats["learned"] += len(signatures)
                fresh.extend(signatures)
            target = assert_writable(done / f"{path.stem}_{int(time.time())}{path.suffix}")
            shutil.move(str(path), str(target))
        if verify_count > 0 and fresh:
            _smoke(db, _units_by_signature(db, fresh, int(verify_count)), stats)
    finally:
        db.close()
    return stats


def watch(folder=None, db_path=None, verify_count: int = 20,
          interval: float = 10.0) -> int:
    """Keep an eye on the inbox and process new files as they arrive."""
    source_dir = Path(folder) if folder else INBOX
    source_dir.mkdir(parents=True, exist_ok=True)
    allowed = TEXT_EXT | {".py", ".json"}
    print("پوشهٔ ورودی زیر نظر است:", source_dir)
    print("هر فایل جدید خودکار خوانده و بررسی می‌شود. Ctrl+C برای توقف.")
    try:
        while True:
            pending = [p for p in source_dir.iterdir()
                       if p.is_file() and p.suffix.lower() in allowed]
            if pending:
                try:
                    report = inbox(source_dir, db_path=db_path,
                                   verify_count=verify_count)
                    print(f"  فایل: {report['files']} | الگو: {report['learned']} "
                          f"| بررسی: {report['checked']} | تأیید: {report['promoted']}")
                except (AgentError, OSError, ValueError) as exc:
                    print("  توقف این دور:", exc)
            time.sleep(max(2.0, float(interval)))
    except KeyboardInterrupt:
        print("نظارت متوقف شد.")
        return 0


def stats(db_path=None, folder=None) -> dict:
    db = knowledge.connect(db_path)
    try:
        info = knowledge.stats(db)
    finally:
        db.close()
    path = Path(db_path) if db_path else knowledge.KNOWLEDGE_DB
    size = path.stat().st_size if path.exists() else 0
    box = Path(folder) if folder else INBOX
    pending = sum(1 for item in box.iterdir() if item.is_file()) if box.exists() else 0
    return {**info, "mb": round(size / 1048576, 2), "inbox_pending": pending}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="brain.ingest", description="خوراک دانش")
    sub = parser.add_subparsers(dest="command", required=True)
    scan_parser = sub.add_parser("scan", help="اسکن فایل‌های پایتون")
    scan_parser.add_argument("--root", action="append", default=None)
    scan_parser.add_argument("--limit", type=int, default=None)
    scan_parser.add_argument("--verify", type=int, default=0, dest="verify_count")
    box_parser = sub.add_parser("inbox", help="پردازش پوشهٔ ai_inbox")
    box_parser.add_argument("--folder", default=None)
    box_parser.add_argument("--verify", type=int, default=20, dest="verify_count")
    watch_parser = sub.add_parser("watch", help="نظارت خودکار بر پوشهٔ ورودی")
    watch_parser.add_argument("--folder", default=None)
    watch_parser.add_argument("--verify", type=int, default=20, dest="verify_count")
    watch_parser.add_argument("--interval", type=float, default=10.0)
    sub.add_parser("stats", help="آمار دانش و صف ورودی")
    args = parser.parse_args(argv)
    try:
        if args.command == "scan":
            roots = args.root or [str(CODE_ROOT)]
            print("ریشه‌ها:", "، ".join(roots))
            report = scan(roots, limit=args.limit, verify_count=args.verify_count)
            print(f"فایل: {report['files']} | الگو: {report['learned']} "
                  f"| ردشده: {report['skipped']}")
            if args.verify_count:
                print(f"بررسی پایه: {report['checked']} | تأییدشده: {report['promoted']}")
            return 0
        if args.command == "inbox":
            report = inbox(args.folder, verify_count=args.verify_count)
            print(f"فایل: {report['files']} | بلوک: {report['blocks']} "
                  f"| الگو: {report['learned']} | نادیده: {report['ignored']}")
            print(f"بررسی پایه: {report['checked']} | تأییدشده: {report['promoted']}")
            print("پوشهٔ ورودی:", args.folder or str(INBOX))
            return 0
        if args.command == "watch":
            return watch(args.folder, verify_count=args.verify_count,
                         interval=args.interval)
        info = stats()
        print(f"الگوها: {info['units']} | تأییدشده: {info['verified']} "
              f"| میانگین وزن: {info['avg_weight']}")
        print(f"حجم: {info['mb']} MB | در صف ورودی: {info['inbox_pending']}")
        return 0
    except (AgentError, OSError, ValueError) as exc:
        print("توقف:", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())