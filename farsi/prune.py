"""Budget-driven pruning of derived statistics.

Text is cheap; the statistics built from it are not. Pruning only activates
when the databases exceed a disk budget, and then removes the least supported
evidence first: singleton pairs, singleton grams, rare unprotected terms, and
frames the user explicitly down-rated. Every touched file is backed up first
and vacuumed after, so space really returns to disk.
"""
from __future__ import annotations

import argparse
import shutil
import sqlite3
import time
from pathlib import Path

from agent.config import ensure_dirs
from agent.errors import AgentError

from . import compose, corpus, frames, lexicon, usage

DEFAULT_BUDGET_MB = 1500.0
_COUNT_CANDIDATES = ("n", "count", "freq", "w", "weight")


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


def _db_path(db: sqlite3.Connection) -> Path:
    for row in db.execute("PRAGMA database_list").fetchall():
        if str(row["name"]) == "main":
            target = str(row["file"] or "")
            if not target:
                raise AgentError("دیتابیس در حافظه است، فایل ندارد")
            return Path(target)
    raise AgentError("دیتابیس اصلی پیدا نشد")


def _sizes(handles: dict) -> tuple[dict, int]:
    out = {}
    total = 0
    for name, db in handles.items():
        path = _db_path(db)
        size = path.stat().st_size if path.exists() else 0
        total += size
        out[name] = (path, size)
    return out, total


def _count_col(db: sqlite3.Connection, table: str) -> str | None:
    cols = [str(row["name"])
            for row in db.execute(f"PRAGMA table_info({table})")]
    if not cols:
        return None
    for cand in _COUNT_CANDIDATES:
        if cand in cols:
            return cand
    return None


def _backup(path: Path) -> Path:
    folder = path.parent / "backups"
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"{path.name}.{time.strftime('%Y%m%d-%H%M%S')}.prune.bak"
    shutil.copy2(path, target)
    return target


def _plan(handles: dict, min_n: float) -> list[dict]:
    udb, fdb, ldb = handles["usage"], handles["frames"], handles["lexicon"]
    stages: list[dict] = []
    for table in ("pairs", "grams"):
        col = _count_col(udb, table)
        if col is None:
            stages.append({"stage": table, "rows": 0,
                           "skipped": "ستون شمارش پیدا نشد"})
            continue
        row = udb.execute(
            f"SELECT COUNT(*) AS n FROM {table} WHERE {col} < ?",
            (min_n,)).fetchone()
        stages.append({"stage": table, "rows": int(row["n"] or 0),
                       "col": col})
    protected = {str(r["term"]) for r in
                 fdb.execute("SELECT DISTINCT term FROM frame_slots")}
    protected |= {str(r["term"]) for r in
                  ldb.execute("SELECT term FROM word_stats")}
    rare = [str(r["term"]) for r in
            udb.execute("SELECT term FROM terms WHERE freq < 2")]
    removable = [term for term in rare if term not in protected]
    stages.append({"stage": "terms", "rows": len(removable),
                   "terms": removable})
    bad = fdb.execute(
        "SELECT fkey FROM frames WHERE score_count > 0 "
        "AND negative > positive").fetchall()
    stages.append({"stage": "frames", "rows": len(bad),
                   "keys": [str(r["fkey"]) for r in bad]})
    return stages


def run(budget_mb: float, min_n: float, assume_yes: bool) -> dict:
    handles = _open_all()
    try:
        sizes, total = _sizes(handles)
        report = {"budget_mb": budget_mb,
                  "total_mb": round(total / 1048576, 2),
                  "stages": _plan(handles, min_n)}
    finally:
        _close_all(handles)
    limit = budget_mb * 1048576
    if total <= limit:
        report["action"] = "none"
        report["message"] = "زیر سقف بودجه؛ هرس لازم نیست."
        return report
    if not assume_yes:
        report["action"] = "dry-run"
        return report
    paths = {name: path for name, (path, _) in sizes.items()}
    backups: dict[str, str] = {}
    for stage in report["stages"]:
        if total <= limit:
            break
        name = stage["stage"]
        if stage.get("skipped") or not stage.get("rows"):
            continue
        owner = "frames" if name == "frames" else "usage"
        if owner not in backups:
            backups[owner] = str(_backup(paths[owner]))
        db = sqlite3.connect(str(paths[owner]))
        try:
            if name in ("pairs", "grams"):
                db.execute(f"DELETE FROM {name} WHERE {stage['col']} < ?",
                           (min_n,))
            elif name == "terms":
                terms = stage["terms"]
                for i in range(0, len(terms), 500):
                    chunk = terms[i:i + 500]
                    marks = ",".join("?" for _ in chunk)
                    db.execute(
                        f"DELETE FROM terms WHERE term IN ({marks})", chunk)
            else:
                keys = stage["keys"]
                for i in range(0, len(keys), 500):
                    chunk = keys[i:i + 500]
                    marks = ",".join("?" for _ in chunk)
                    db.execute(
                        f"DELETE FROM frame_slots WHERE fkey IN ({marks})",
                        chunk)
                    db.execute(
                        f"DELETE FROM frames WHERE fkey IN ({marks})", chunk)
            db.commit()
            db.execute("VACUUM")
        finally:
            db.close()
        stage["removed"] = stage["rows"]
        total = sum(path.stat().st_size if path.exists() else 0
                    for path in paths.values())
    report["action"] = "pruned"
    report["backups"] = backups
    report["total_mb"] = round(total / 1048576, 2)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="farsi.prune", description="هرس بودجه‌ای آمار")
    parser.add_argument("--budget-mb", type=float, default=DEFAULT_BUDGET_MB)
    parser.add_argument("--min-n", type=float, default=2.0)
    parser.add_argument("--yes", action="store_true")
    args = parser.parse_args(argv)
    try:
        report = run(args.budget_mb, args.min_n, args.yes)
    except (AgentError, OSError, ValueError, sqlite3.Error) as exc:
        print("توقف:", exc)
        return 1
    print("حجم کل:", report["total_mb"], "MB | سقف:", report["budget_mb"])
    for stage in report["stages"]:
        if stage.get("skipped"):
            print(f"  {stage['stage']}: رد شد ({stage['skipped']})")
        else:
            print(f"  {stage['stage']}: {stage['rows']} ردیف قابل حذف")
    print("اقدام:", report.get("action"))
    if report.get("message"):
        print(report["message"])
    if report.get("backups"):
        print("بکاپ‌ها:", report["backups"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())