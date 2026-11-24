"""Fast single-key drill for rating composed candidates.

Each round picks a topic that already has mined frames, composes candidates,
and waits for one key. Ratings go through compose.rate, so the drill and the
Tkinter UI share one memory. Frame keys rejected in this session are excluded
from later rounds, so "reject both" really yields something new.
"""
from __future__ import annotations

import argparse
import sqlite3

from agent.errors import AgentError

from . import compose, corpus, frames, lexicon, usage

HELP = ("کلیدها: 1 یا 2 = انتخاب | ر = رد هر دو | "
        "س یا هر کلید دیگر = رد بدون امتیاز | خ = خروج")


def _read_key() -> str:
    try:
        from msvcrt import getwch
    except ImportError:
        return input("کلید: ").strip() or "س"
    while True:
        char = getwch()
        if char in "\r\n":
            continue
        return char


def _pick_topic(fdb: sqlite3.Connection, ldb: sqlite3.Connection,
                used: set[str]) -> str | None:
    rows = fdb.execute(
        "SELECT DISTINCT term FROM frame_slots "
        "ORDER BY RANDOM() LIMIT 40"
    ).fetchall()
    for row in rows:
        term = str(row["term"])
        if term in used:
            continue
        if lexicon.word_weight(ldb, term) <= 0.1:
            continue
        return term
    return None


def run(rounds: int) -> dict:
    cdb = corpus.connect()
    udb = usage.connect()
    ldb = lexicon.connect()
    fdb = frames.connect()
    tdb = compose.connect()

    rejected: set[str] = set()
    used_topics: set[str] = set()
    tally = {"rated": 0, "rejected": 0, "skipped": 0, "empty": 0}

    try:
        print(HELP)
        for number in range(1, max(1, rounds) + 1):
            topic = _pick_topic(fdb, ldb, used_topics)
            if topic is None:
                print("\nموضوع تازه‌ای نماند؛ پایان.")
                break
            used_topics.add(topic)

            print(f"\n--- دور {number} | موضوع: "
                  f"{lexicon.surface_for(ldb, topic)} ---")
            report = compose.compose(
                tdb, cdb, udb, ldb, fdb,
                f"یه جمله درباره {topic} بگو",
                exclude=rejected,
            )
            candidates = report.get("candidates") or []
            if not candidates:
                tally["empty"] += 1
                print("نامزدی نبود:", report.get("reason") or "-")
                continue

            for index, item in enumerate(candidates, start=1):
                print(f"  {index}. {item['text']}")

            key = _read_key().strip()
            offer_id = report.get("offer_id")

            if key in ("1", "2") and len(candidates) >= int(key):
                compose.rate(tdb, fdb, ldb, offer_id, choice=int(key))
                tally["rated"] += 1
                print("ثبت شد.")
            elif key in ("ر", "r"):
                scores = {int(item["position"]): 1.0 for item in candidates}
                compose.rate(tdb, fdb, ldb, offer_id, scores=scores)
                for item in candidates:
                    rejected.add(str(item["fkey"]))
                tally["rejected"] += 1
                print("هر دو رد شدند؛ دور بعد نامزد تازه می‌بینی.")
            elif key in ("خ", "q"):
                print("پایان با درخواست تو.")
                break
            else:
                tally["skipped"] += 1
                print("رد شد، بدون امتیاز.")

        return tally
    finally:
        for db in (tdb, fdb, ldb, udb, cdb):
            try:
                db.close()
            except sqlite3.Error:
                pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="farsi.drill", description="دریل امتیازدهی سریع")
    parser.add_argument("--rounds", type=int, default=10)
    args = parser.parse_args(argv)

    try:
        tally = run(args.rounds)
    except (AgentError, OSError, ValueError, sqlite3.Error) as exc:
        print("توقف:", exc)
        return 1

    print("خلاصه:", tally)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())