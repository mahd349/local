"""Resumable, throttled build driver.

Stages run in dependency order and stop when a stage reports no new documents.
Every stage keeps its own marker, so the script can be run again at any time and
continues where it stopped.

Heat control is a duty cycle: after each work slice the process sleeps for
work * (1 - duty) / duty, so average utilisation stays near the requested
fraction. Process priority is lowered as well, which keeps the desktop
responsive without changing the duty maths.

The disk guard stops before the configured budget is exceeded, because the
co-occurrence tables grow much faster than the other layers.
"""
from __future__ import annotations

import argparse
import ctypes
import os
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, r"D:\Python")
sys.path.insert(0, r"D:\project\library")

from agent.config import STATE_DIR

from . import corpus, fastbuild, frames, lexicon, usage

BELOW_NORMAL_PRIORITY_CLASS = 0x00004000


class Throttle:
    def __init__(self, duty: float) -> None:
        self.duty = max(0.05, min(1.0, float(duty)))

    def pause(self, work_seconds: float) -> float:
        if self.duty >= 0.999 or work_seconds <= 0:
            return 0.0
        nap = work_seconds * (1.0 - self.duty) / self.duty
        time.sleep(nap)
        return nap


def lower_priority() -> str:
    if sys.platform != "win32":
        try:
            os.nice(10)
            return "nice+10"
        except (OSError, AttributeError):
            return "unchanged"
    try:
        kernel32 = ctypes.windll.kernel32
        kernel32.GetCurrentProcess.restype = ctypes.c_void_p
        kernel32.SetPriorityClass.argtypes = (ctypes.c_void_p, ctypes.c_uint32)
        kernel32.SetPriorityClass.restype = ctypes.c_int
        handle = kernel32.GetCurrentProcess()
        ok = kernel32.SetPriorityClass(handle, BELOW_NORMAL_PRIORITY_CLASS)
        return "below-normal" if ok else "unchanged"
    except (AttributeError, OSError):
        return "unchanged"


def disk_mb() -> float:
    total = 0
    for path in Path(STATE_DIR).glob("*"):
        if not path.is_file():
            continue
        if path.suffix == ".db" or path.name.endswith(("-wal", "-shm")):
            total += path.stat().st_size
    return total / 1024 / 1024


def plain_marker(db: sqlite3.Connection, key: str = "last_doc") -> int:
    try:
        row = db.execute("SELECT mvalue FROM meta WHERE mkey=?", (key,)).fetchone()
    except sqlite3.Error:
        return 0
    if row is None:
        return 0
    try:
        return int(str(row[0]))
    except (TypeError, ValueError):
        return 0


def status(cdb, udb, ldb, fdb) -> dict:
    row = cdb.execute("SELECT COUNT(*) AS n, MAX(id) AS m FROM docs").fetchone()
    out = {
        "corpus_rows": int(row["n"] or 0),
        "corpus_max_id": int(row["m"] or 0),
        "usage_last_doc": int(fastbuild.read_meta(udb, "last_doc", 0)),
        "lexicon_last_doc": plain_marker(ldb),
        "frames_last_doc": plain_marker(fdb),
    }
    path = Path(STATE_DIR) / "farsi_syntax.db"
    if path.exists():
        try:
            sdb = sqlite3.connect(str(path))
            sdb.execute("PRAGMA busy_timeout=60000")
            out["syntax_last_doc"] = plain_marker(sdb)
            sdb.close()
        except sqlite3.Error:
            out["syntax_last_doc"] = -1
    out["disk_mb"] = round(disk_mb(), 1)
    return out


def drive(label: str, call, throttle: Throttle, budget_mb: float) -> dict:
    total = rounds = 0
    started = time.time()
    while True:
        rounds += 1
        moment = time.time()
        report = call()
        work = time.time() - moment
        if report.get("skipped"):
            print(f"[{label}] skipped: {report['skipped']}")
            break
        done = int(report.get("docs", 0) or 0)
        total += done
        size = disk_mb()
        print(f"[{label}] round {rounds}: docs={done:,} total={total:,} "
              f"work={work:.1f}s disk={size:.0f}MB")
        if done == 0:
            break
        if size >= budget_mb:
            print(f"[{label}] stopped: disk budget {budget_mb:.0f}MB reached")
            break
        nap = throttle.pause(work)
        if nap:
            print(f"[{label}] cool-down {nap:.1f}s")
    return {"rounds": rounds, "docs": total,
            "seconds": round(time.time() - started, 1)}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="farsi.build_all",
                                description="ساخت مرحله‌ای با محدودیت مصرف")
    p.add_argument("--stages", default="lexicon",
                   help="comma list: usage,lexicon,frames")
    p.add_argument("--duty", type=float, default=0.75)
    p.add_argument("--slice-seconds", type=float, default=20.0)
    p.add_argument("--max-db-gb", type=float, default=2.0)
    p.add_argument("--window", type=int, default=4)
    p.add_argument("--batch-docs", type=int, default=4000)
    p.add_argument("--cache-mb", type=int, default=384)
    p.add_argument("--phrases", action="store_true")
    p.add_argument("--status", action="store_true")
    p.add_argument("--reindex-only", action="store_true")
    p.add_argument("--normal-priority", action="store_true")
    a = p.parse_args(argv)

    if not corpus.learning_enabled():
        print("learning is off; enable it first")
        return 1

    stages = [s.strip() for s in a.stages.split(",") if s.strip()]
    unknown = [s for s in stages if s not in ("usage", "lexicon", "frames")]
    if unknown:
        print("unknown stages:", ", ".join(unknown))
        return 1

    throttle = Throttle(a.duty)
    priority = "normal" if a.normal_priority else lower_priority()
    budget_mb = a.max_db_gb * 1024

    cdb = corpus.connect()
    udb = fastbuild.open_db(a.cache_mb)
    ldb = lexicon.connect()
    fdb = frames.connect()
    dropped = False
    summary: dict[str, dict] = {}

    try:
        print(f"priority={priority} duty={throttle.duty:.2f} "
              f"slice={a.slice_seconds:.0f}s budget={budget_mb:.0f}MB")
        before = status(cdb, udb, ldb, fdb)
        for key, value in before.items():
            print(f"  {key}: {value}")
        if a.status:
            return 0
        if a.reindex_only:
            print(fastbuild.make_indexes(udb))
            return 0

        if "usage" in stages:
            fastbuild.drop_indexes(udb)
            dropped = True
            print("secondary indexes dropped for the bulk load")
            summary["usage"] = drive(
                "usage",
                lambda: fastbuild.build(cdb, udb, window=a.window,
                                        batch_docs=a.batch_docs,
                                        max_seconds=a.slice_seconds,
                                        limit_docs=None),
                throttle, budget_mb)

        if "lexicon" in stages:
            summary["lexicon"] = drive(
                "lexicon",
                lambda: lexicon.harvest(ldb, cdb,
                                        max_seconds=a.slice_seconds),
                throttle, budget_mb)

        if "frames" in stages:
            summary["frames"] = drive(
                "frames",
                lambda: frames.mine(fdb, cdb, udb,
                                    max_seconds=a.slice_seconds),
                throttle, budget_mb)

        if a.phrases:
            moment = time.time()
            found = usage.discover_phrases(udb, limit=200)
            print(f"[phrases] {len(found)} in {round(time.time() - moment, 1)}s")
            throttle.pause(time.time() - moment)

        return 0
    except KeyboardInterrupt:
        print("\ninterrupted; markers are saved, run again to continue")
        return 130
    finally:
        if dropped:
            print("rebuilding secondary indexes (needed by generation)...")
            try:
                print(fastbuild.make_indexes(udb))
            except sqlite3.Error as exc:
                print("index rebuild failed:", exc)
                print("run: python -m farsi.build_all --reindex-only")
        after = status(cdb, udb, ldb, fdb)
        print("\nfinal status:")
        for key, value in after.items():
            print(f"  {key}: {value}")
        for stage, info in summary.items():
            print(f"  {stage}: {info}")
        fdb.close()
        ldb.close()
        udb.close()
        cdb.close()


if __name__ == "__main__":
    raise SystemExit(main())