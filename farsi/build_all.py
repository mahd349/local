"""Drive the build pipeline past its hidden per-call time caps.

usage.build defaults to max_seconds=120 and the CLI does not expose it, so a
single invocation silently stops early. Every stage here resumes from its own
last_doc marker, so we loop until a stage reports zero new documents.
"""
from __future__ import annotations

import argparse
import sys
import time

sys.path.insert(0, r"D:\Python")
sys.path.insert(0, r"D:\project\library")

from farsi import corpus, frames, lexicon, usage

STAGE_SECONDS = 900.0
MAX_ROUNDS = 400


def loop(label: str, call, budget: float) -> dict:
    total_docs = 0
    rounds = 0
    started = time.time()
    while rounds < MAX_ROUNDS:
        rounds += 1
        report = call(budget)
        if report.get("skipped"):
            print(f"[{label}] skipped: {report['skipped']}")
            return {"rounds": rounds, "docs": total_docs,
                    "seconds": round(time.time() - started, 2),
                    "skipped": report["skipped"]}
        done = int(report.get("docs", 0) or 0)
        total_docs += done
        print(f"[{label}] round {rounds}: docs={done} "
              f"total={total_docs} {report.get('seconds', 0)}s")
        if done == 0:
            break
    return {"rounds": rounds, "docs": total_docs,
            "seconds": round(time.time() - started, 2)}


def main() -> int:
    parser = argparse.ArgumentParser(prog="build_all")
    parser.add_argument("--budget", type=float, default=STAGE_SECONDS)
    parser.add_argument("--window", type=int, default=4)
    parser.add_argument("--skip-frames", action="store_true")
    args = parser.parse_args()

    cdb = corpus.connect()
    udb = usage.connect()
    ldb = lexicon.connect()
    fdb = frames.connect()

    summary: dict[str, dict] = {}
    try:
        rows = cdb.execute("SELECT COUNT(*) AS n FROM docs").fetchone()["n"]
        print(f"corpus rows: {rows:,}")
        if rows == 0:
            print("Nothing to build. Run ingest_wiki.py first.")
            return 1

        summary["usage"] = loop(
            "usage", lambda b: usage.build(udb, cdb, window=args.window,
                                           max_seconds=b), args.budget)

        print("[usage] discovering phrases...")
        phrases = usage.discover_phrases(udb, limit=200)
        print(f"[usage] phrases stored, top={len(phrases)}")

        summary["lexicon"] = loop(
            "lexicon", lambda b: lexicon.harvest(ldb, cdb, max_seconds=b),
            args.budget)

        if not args.skip_frames:
            summary["frames"] = loop(
                "frames", lambda b: frames.mine(fdb, cdb, udb, max_seconds=b),
                args.budget)

        print("\n" + "=" * 66)
        print("USAGE:", usage.stats(udb))
        print("LEXICON:", lexicon.stats(ldb))
        print("FRAMES:", frames.stats(fdb))
        print("=" * 66)
        for stage, info in summary.items():
            print(f"{stage:<10} {info}")
        return 0
    finally:
        fdb.close()
        ldb.close()
        udb.close()
        cdb.close()


if __name__ == "__main__":
    raise SystemExit(main())