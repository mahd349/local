"""Forget source documents and rebuild every derived store.

Counts, surface forms, sentence df and mined frames are aggregates: they cannot
be subtracted per document. Forgetting a document therefore means deleting it
and rebuilding. Every file touched is backed up first, and word-level feedback
is preserved unless explicitly dropped.
"""
from __future__ import annotations

import argparse
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

from agent.config import ensure_dirs
from agent.errors import AgentError

from . import compose, corpus, frames, lexicon, usage

PACKAGE_PARENT = Path(__file__).resolve().parent.parent


def _db_path(db: sqlite3.Connection) -> Path:
    for row in db.execute("PRAGMA database_list").fetchall():
        if str(row["name"]) == "main":
            target = str(row["file"] or "")
            if not target:
                raise AgentError("دیتابیس در حافظه است، فایل ندارد")
            return Path(target)
    raise AgentError("دیتابیس اصلی پیدا نشد")


def _open_all() -> dict:
    ensure_dirs()
    return {"corpus": corpus.connect(), "usage": usage.connect(),
            "lexicon": lexicon.connect(), "frames": frames.connect(),
            "compose": compose.connect()}


def _close_all(handles: dict) -> None:
    for db in handles.values():
        try:
            db.close()
        except sqlite3.Error:
            pass


def list_docs(cdb: sqlite3.Connection, limit: int = 20) -> list[dict]:
    rows = cdb.execute("SELECT * FROM docs ORDER BY id DESC LIMIT ?",
                       (max(1, int(limit)),)).fetchall()
    out = []
    for row in rows:
        item = dict(row)
        raw = str(item.get("raw") or "")
        out.append({"id": int(item["id"]), "source": item.get("source"),
                    "speaker": item.get("speaker"), "chars": len(raw),
                    "preview": " ".join(raw[:90].split())})
    return out


def _backup(path: Path) -> Path:
    folder = path.parent / "backups"
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"{path.name}.{time.strftime('%Y%m%d-%H%M%S')}.bak"
    shutil.copy2(path, target)
    return target


def _vacuum(path: Path) -> None:
    db = sqlite3.connect(str(path))
    try:
        db.execute("VACUUM")
    finally:
        db.close()


def forget(ids: list[int], *, reset_usage: bool = False,
           drop_feedback: bool = False, assume_yes: bool = False) -> dict:
    handles = _open_all()
    try:
        wanted = sorted({int(value) for value in ids})
        if not wanted:
            raise AgentError("شناسهٔ سند لازم است")
        marks = ",".join("?" for _ in wanted)
        found = [int(row["id"]) for row in handles["corpus"].execute(
            f"SELECT id FROM docs WHERE id IN ({marks})", wanted).fetchall()]
        missing = [value for value in wanted if value not in found]
        if missing:
            raise AgentError("سند پیدا نشد: " + ", ".join(map(str, missing)))

        paths = {name: _db_path(db) for name, db in handles.items()}
        plan = {"documents": found,
                "clears": ["corpus.docs (rows)", "lexicon.surface", "lexicon.df",
                           "frames.frames", "frames.frame_slots",
                           "compose.offers", "compose.candidates"],
                "usage_reset": bool(reset_usage),
                "drop_feedback": bool(drop_feedback),
                "files": {name: str(path) for name, path in paths.items()}}
        if not assume_yes:
            return {"dry_run": True, "plan": plan}
    finally:
        _close_all(handles)

    backups = {}
    touched = ["corpus", "lexicon", "frames", "compose"]
    if reset_usage:
        touched.append("usage")
    for name in touched:
        backups[name] = str(_backup(paths[name]))

    db = sqlite3.connect(str(paths["corpus"]))
    try:
        db.execute(f"DELETE FROM docs WHERE id IN ({marks})", wanted)
        db.commit()
    finally:
        db.close()

    db = sqlite3.connect(str(paths["lexicon"]))
    try:
        db.execute("DELETE FROM surface")
        db.execute("DELETE FROM df")
        db.execute("DELETE FROM meta")
        if drop_feedback:
            db.execute("DELETE FROM word_stats")
        db.commit()
    finally:
        db.close()

    db = sqlite3.connect(str(paths["frames"]))
    try:
        db.execute("DELETE FROM frame_slots")
        db.execute("DELETE FROM frames")
        db.execute("DELETE FROM meta")
        db.commit()
    finally:
        db.close()

    db = sqlite3.connect(str(paths["compose"]))
    try:
        db.execute("DELETE FROM candidates")
        db.execute("DELETE FROM offers")
        if drop_feedback:
            db.execute("DELETE FROM assoc")
        db.commit()
    finally:
        db.close()

    if reset_usage:
        paths["usage"].unlink()
        usage.connect().close()

    for name in touched:
        _vacuum(paths[name])

    return {"dry_run": False, "forgotten": found, "backups": backups,
            "usage_reset": bool(reset_usage),
            "next": "python -m farsi.purge rebuild"}


def _run(module: str, *args: str) -> int:
    command = [sys.executable, "-m", module, *args]
    print("\n$", " ".join(command))
    done = subprocess.run(command, cwd=str(PACKAGE_PARENT),
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace")
    if done.stdout:
        print(done.stdout.strip())
    if done.stderr:
        print(done.stderr.strip())
    return int(done.returncode)


def rebuild() -> dict:
    steps = [("farsi.usage", ("build",)),
             ("farsi.lexicon", ("harvest", "--rebuild")),
             ("farsi.frames", ("mine",))]
    codes = {}
    for module, args in steps:
        code = _run(module, *args)
        codes[module] = code
        if code != 0:
            return {"stopped_at": module, "codes": codes}
    return {"codes": codes, "done": True}


def size() -> dict:
    handles = _open_all()
    try:
        rows = {}
        total = 0
        for name, db in handles.items():
            path = _db_path(db)
            bytes_used = path.stat().st_size if path.exists() else 0
            total += bytes_used
            rows[name] = {"file": str(path), "mb": round(bytes_used / 1048576, 2)}
        counts = {"usage": usage.stats(handles["usage"]),
                  "lexicon": lexicon.stats(handles["lexicon"]),
                  "frames": frames.stats(handles["frames"]),
                  "compose": compose.stats(handles["compose"])}
        return {"databases": rows, "total_mb": round(total / 1048576, 2),
                "counts": counts}
    finally:
        _close_all(handles)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="farsi.purge", description="فراموشی سند، بازسازی و اندازه")
    sub = parser.add_subparsers(dest="command", required=True)

    dc = sub.add_parser("docs", help="فهرست سندهای پیکره")
    dc.add_argument("--limit", type=int, default=20)

    fg = sub.add_parser("forget", help="حذف سند و پاک‌سازی مشتقات")
    fg.add_argument("--doc", action="append", type=int, default=[])
    fg.add_argument("--all", action="store_true")
    fg.add_argument("--usage", action="store_true",
                    help="دیتابیس usage هم از نو ساخته شود")
    fg.add_argument("--drop-feedback", action="store_true")
    fg.add_argument("--yes", action="store_true")
    fg.add_argument("--rebuild", action="store_true")

    sub.add_parser("rebuild", help="بازسازی usage و lexicon و frames")
    sub.add_parser("size", help="حجم دیسک و شمارش‌ها")

    args = parser.parse_args(argv)

    try:
        if args.command == "docs":
            cdb = corpus.connect()
            try:
                for item in list_docs(cdb, args.limit):
                    print(f"[{item['id']}] {item['chars']} نویسه "
                          f"| source={item['source']} | {item['preview']}")
            finally:
                cdb.close()
            return 0

        if args.command == "forget":
            ids = list(args.doc)
            if args.all:
                cdb = corpus.connect()
                try:
                    ids = [item["id"] for item in list_docs(cdb, 100000)]
                finally:
                    cdb.close()
            report = forget(ids, reset_usage=args.usage,
                            drop_feedback=args.drop_feedback,
                            assume_yes=args.yes)
            if report.get("dry_run"):
                print("برنامهٔ اجرا (برای انجام، --yes بده):")
                for key, value in report["plan"].items():
                    print(f"  {key}: {value}")
                return 0
            print("حذف شد:", report["forgotten"])
            print("بکاپ‌ها:", report["backups"])
            if args.rebuild:
                print(rebuild())
            else:
                print("بعدی:", report["next"])
            return 0

        if args.command == "rebuild":
            report = rebuild()
            print(report)
            return 0 if report.get("done") else 1

        report = size()
        for name, item in report["databases"].items():
            print(f"{name:9} {item['mb']:8} MB  {item['file']}")
        print("جمع:", report["total_mb"], "MB")
        for name, item in report["counts"].items():
            print(f"  {name}: {item}")
        return 0

    except (AgentError, OSError, ValueError, sqlite3.Error) as exc:
        print("توقف:", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())