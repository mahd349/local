"""Separate real data size from free pages, and compact a database on request.

DELETE removes rows but keeps the file size, so a budget decision based on file
size alone is misleading. page_count and freelist_count tell the two apart.
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, r"D:\Python")
sys.path.insert(0, r"D:\project\library")

from agent.config import STATE_DIR

MB = 1024 * 1024


def report() -> list[dict]:
    rows: list[dict] = []
    for path in sorted(Path(STATE_DIR).glob("*.db")):
        entry = {"name": path.name, "file_mb": path.stat().st_size / MB,
                 "data_mb": 0.0, "free_mb": 0.0, "wal_mb": 0.0}
        try:
            db = sqlite3.connect(str(path))
            db.execute("PRAGMA busy_timeout=60000")
            page = int(db.execute("PRAGMA page_size").fetchone()[0])
            pages = int(db.execute("PRAGMA page_count").fetchone()[0])
            free = int(db.execute("PRAGMA freelist_count").fetchone()[0])
            db.close()
            entry["data_mb"] = (pages - free) * page / MB
            entry["free_mb"] = free * page / MB
        except sqlite3.Error as exc:
            entry["error"] = str(exc)
        for suffix in ("-wal", "-shm"):
            side = Path(str(path) + suffix)
            if side.exists():
                entry["wal_mb"] += side.stat().st_size / MB
        rows.append(entry)
    return rows


def vacuum(name: str) -> dict:
    path = Path(STATE_DIR) / name
    if not path.exists():
        raise SystemExit(f"not found: {path}")
    before = path.stat().st_size / MB
    started = time.time()
    db = sqlite3.connect(str(path))
    db.isolation_level = None
    db.execute("PRAGMA busy_timeout=120000")
    db.execute("VACUUM")
    db.close()
    after = path.stat().st_size / MB
    return {"name": name, "before_mb": round(before, 1),
            "after_mb": round(after, 1),
            "reclaimed_mb": round(before - after, 1),
            "seconds": round(time.time() - started, 1)}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="farsi.dbsize",
                                description="سنجش فضای واقعی پایگاه‌ها")
    p.add_argument("--vacuum", default=None, help="نام فایل پایگاه برای فشرده‌سازی")
    p.add_argument("--budget-gb", type=float, default=3.5)
    a = p.parse_args(argv)

    if a.vacuum:
        print(vacuum(a.vacuum))
        return 0

    rows = report()
    totals = {"file": 0.0, "data": 0.0, "free": 0.0, "wal": 0.0}
    print(f"{'database':<26}{'file':>10}{'data':>10}{'free':>10}{'journal':>10}")
    for row in sorted(rows, key=lambda item: -item["file_mb"]):
        totals["file"] += row["file_mb"]
        totals["data"] += row["data_mb"]
        totals["free"] += row["free_mb"]
        totals["wal"] += row["wal_mb"]
        note = "  " + row["error"] if "error" in row else ""
        print(f"{row['name']:<26}{row['file_mb']:>9.1f}M{row['data_mb']:>9.1f}M"
              f"{row['free_mb']:>9.1f}M{row['wal_mb']:>9.1f}M{note}")
    print("-" * 66)
    print(f"{'total':<26}{totals['file']:>9.1f}M{totals['data']:>9.1f}M"
          f"{totals['free']:>9.1f}M{totals['wal']:>9.1f}M")
    used = totals["file"] + totals["wal"]
    print(f"\nused now: {used:.0f}MB   budget: {a.budget_gb * 1024:.0f}MB   "
          f"headroom: {a.budget_gb * 1024 - used:.0f}MB")
    print(f"reclaimable by compacting: {totals['free']:.0f}MB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())