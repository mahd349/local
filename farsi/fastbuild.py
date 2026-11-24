"""Fast, interrupt-safe rebuild of the usage statistics.

Semantics are identical to usage.build: same weights, same window pairs, same
totals. Only the write strategy changes:
  * secondary indexes are dropped during the bulk load and rebuilt at the end
  * counts aggregate over thousands of documents, so hot words upsert once
  * each batch is sorted by key, turning random B-tree writes into sequential
  * last_doc advances on EVERY flush, so Ctrl+C never double-counts

The indexes are required by usage.neighbors and usage.next_options, so a run
that ends without them must be finished with --reindex-only.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, r"D:\Python")
sys.path.insert(0, r"D:\project\library")

from agent.config import STATE_DIR

from . import corpus

USAGE_DB = STATE_DIR / "farsi_usage.db"

INDEXES = (
    ("idx_pairs_a", "CREATE INDEX IF NOT EXISTS idx_pairs_a ON pairs(a, n DESC)"),
    ("idx_pairs_b", "CREATE INDEX IF NOT EXISTS idx_pairs_b ON pairs(b, n DESC)"),
    ("idx_grams_prev", "CREATE INDEX IF NOT EXISTS idx_grams_prev ON grams(prev, n DESC)"),
)
TERMS_SQL = ("INSERT INTO terms(term,freq,docs,updated_ts) VALUES(?,?,?,?) "
             "ON CONFLICT(term) DO UPDATE SET freq=freq+excluded.freq, "
             "docs=docs+excluded.docs, updated_ts=excluded.updated_ts")
PAIRS_SQL = ("INSERT INTO pairs(a,b,n) VALUES(?,?,?) "
             "ON CONFLICT(a,b) DO UPDATE SET n=n+excluded.n")
GRAMS_SQL = ("INSERT INTO grams(prev,nxt,n) VALUES(?,?,?) "
             "ON CONFLICT(prev,nxt) DO UPDATE SET n=n+excluded.n")

EMPTY_TOTALS = {"tokens": 0.0, "bigrams": 0.0, "trigrams": 0.0, "windows": 0.0}


def open_db(cache_mb: int) -> sqlite3.Connection:
    db = sqlite3.connect(str(USAGE_DB))
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA busy_timeout=60000")
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA synchronous=NORMAL")
    db.execute(f"PRAGMA cache_size=-{int(cache_mb) * 1024}")
    db.execute("PRAGMA mmap_size=1073741824")
    db.execute("PRAGMA temp_store=MEMORY")
    return db


def read_meta(db: sqlite3.Connection, key: str, default):
    row = db.execute("SELECT mvalue FROM meta WHERE mkey=?", (key,)).fetchone()
    return json.loads(row["mvalue"]) if row else default


def write_meta(db: sqlite3.Connection, key: str, value) -> None:
    db.execute("INSERT INTO meta(mkey,mvalue) VALUES(?,?) "
               "ON CONFLICT(mkey) DO UPDATE SET mvalue=excluded.mvalue",
               (key, json.dumps(value, ensure_ascii=False)))


def index_state(db: sqlite3.Connection) -> dict[str, bool]:
    present = {str(r["name"]) for r in db.execute(
        "SELECT name FROM sqlite_master WHERE type='index'")}
    return {name: name in present for name, _ in INDEXES}


def drop_indexes(db: sqlite3.Connection) -> None:
    for name, _ in INDEXES:
        db.execute(f"DROP INDEX IF EXISTS {name}")
    db.commit()


def make_indexes(db: sqlite3.Connection) -> dict:
    started = time.time()
    done = []
    for name, sql in INDEXES:
        t0 = time.time()
        for attempt in range(5):
            try:
                db.execute(sql)
                db.commit()
                break
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc) or attempt == 4:
                    raise
                print(f"  {name} locked, retry {attempt + 1}/5 in 10s")
                time.sleep(10)
        done.append(name)
        print(f"  index {name} built in {round(time.time() - t0, 1)}s")
    db.execute("ANALYZE")
    db.commit()
    return {"indexes": done, "seconds": round(time.time() - started, 2)}
    db.execute("ANALYZE")
    db.commit()
    return {"indexes": done, "seconds": round(time.time() - started, 2)}


def reset(db: sqlite3.Connection) -> None:
    for table in ("terms", "pairs", "grams", "phrases"):
        db.execute(f"DELETE FROM {table}")
    write_meta(db, "last_doc", 0)
    write_meta(db, "totals", dict(EMPTY_TOTALS))
    db.commit()
    db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    print("usage tables cleared, last_doc=0")


def build(cdb: sqlite3.Connection, db: sqlite3.Connection, *, window: int,
          batch_docs: int, max_seconds: float, limit_docs: int | None) -> dict:
    last = int(read_meta(db, "last_doc", 0))
    totals = read_meta(db, "totals", dict(EMPTY_TOTALS))
    remaining = int(cdb.execute(
        "SELECT COUNT(*) AS n FROM docs WHERE id>?", (last,)).fetchone()["n"])
    print(f"resume from last_doc={last}, documents remaining={remaining:,}")
    if remaining == 0:
        return {"docs": 0, "seconds": 0.0, "rate": 0.0}

    terms: Counter = Counter()
    docfreq: Counter = Counter()
    pairs: Counter = Counter()
    grams: Counter = Counter()

    started = time.time()
    interval_start = started
    interval_docs = 0
    docs = seen_tokens = 0
    newest = last
    flushes = 0

    def flush() -> None:
        nonlocal flushes
        now = time.time()
        if terms:
            db.executemany(TERMS_SQL,
                           [(t, n, docfreq.get(t, 0), now)
                            for t, n in sorted(terms.items())])
        if pairs:
            db.executemany(PAIRS_SQL,
                           [(a, b, n) for (a, b), n in sorted(pairs.items())])
        if grams:
            db.executemany(GRAMS_SQL,
                           [(p, x, n) for (p, x), n in sorted(grams.items())])
        write_meta(db, "totals", totals)
        write_meta(db, "last_doc", newest)
        db.commit()
        terms.clear(); docfreq.clear(); pairs.clear(); grams.clear()
        flushes += 1

    cursor = cdb.execute(
        "SELECT id, keys, quality, weight FROM docs WHERE id>? ORDER BY id",
        (last,))
    for row in cursor:
        words = (row["keys"] or "").split()
        newest = max(newest, int(row["id"]))
        if not words:
            continue
        weight = float(row["weight"] or 1.0) * max(0.2, float(row["quality"] or 0.5))
        docs += 1
        interval_docs += 1
        seen_tokens += len(words)

        for word in words:
            terms[word] += weight
        for word in set(words):
            docfreq[word] += 1
        for index in range(len(words) - 1):
            grams[(words[index], words[index + 1])] += weight
        for index in range(len(words) - 2):
            grams[(words[index] + " " + words[index + 1], words[index + 2])] += weight
        for index, center in enumerate(words):
            low = max(0, index - window)
            high = min(len(words), index + window + 1)
            for other in range(low, high):
                if other != index:
                    pairs[(center, words[other])] += weight
                    totals["windows"] += weight
        totals["tokens"] += weight * len(words)
        totals["bigrams"] += weight * max(0, len(words) - 1)
        totals["trigrams"] += weight * max(0, len(words) - 2)

        if docs % batch_docs == 0:
            flush()
            elapsed = time.time() - interval_start
            rate = interval_docs / max(1e-9, elapsed)
            left = max(0, remaining - docs)
            eta = left / rate if rate > 0 else 0.0
            print(f"  docs={docs:,}/{remaining:,} tokens={seen_tokens:,} "
                  f"flush={round(elapsed, 1)}s rate={rate:.1f} doc/s "
                  f"eta={round(eta / 60, 1)} min")
            interval_start = time.time()
            interval_docs = 0

        if limit_docs and docs >= limit_docs:
            break
        if time.time() - started > max_seconds:
            break

    flush()
    seconds = time.time() - started
    return {"docs": docs, "tokens": seen_tokens, "flushes": flushes,
            "seconds": round(seconds, 2),
            "rate": round(docs / max(1e-9, seconds), 2),
            "last_doc": newest}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="farsi.fastbuild",
                                description="ساخت سریع و قابل‌قطع آمار هم‌رخدادی")
    p.add_argument("--window", type=int, default=4)
    p.add_argument("--batch-docs", type=int, default=4000)
    p.add_argument("--cache-mb", type=int, default=384)
    p.add_argument("--max-seconds", type=float, default=86400.0)
    p.add_argument("--limit-docs", type=int, default=None)
    p.add_argument("--reset", action="store_true")
    p.add_argument("--keep-indexes", action="store_true")
    p.add_argument("--reindex-only", action="store_true")
    a = p.parse_args(argv)

    if not corpus.learning_enabled():
        print("learning is off; enable it first")
        return 1

    db = open_db(a.cache_mb)
    cdb = corpus.connect()
    try:
        print("index state:", index_state(db))
        if a.reindex_only:
            print(make_indexes(db))
            return 0
        if a.reset:
            reset(db)
        if not a.keep_indexes:
            drop_indexes(db)
            print("secondary indexes dropped for the bulk load")

        report = build(cdb, db, window=a.window, batch_docs=a.batch_docs,
                       max_seconds=a.max_seconds, limit_docs=a.limit_docs)
        print("\nbuild:", report)

        if report["docs"]:
            print("rebuilding indexes (required by neighbors/next_options)...")
            print(make_indexes(db))
            from . import usage
            print("discovering phrases...")
            found = usage.discover_phrases(db, limit=200)
            print(f"phrases: {len(found)}")

        rows = {t: db.execute(f"SELECT COUNT(*) AS n FROM {t}").fetchone()["n"]
                for t in ("terms", "pairs", "grams", "phrases")}
        size = Path(USAGE_DB).stat().st_size / 1024 / 1024
        print("rows:", rows)
        print(f"database size: {size:.1f} MB")
        print("index state:", index_state(db))
        if report["rate"] and report["rate"] < 20:
            print("VERDICT: below 20 doc/s -> move this stage to cloud compute.")
        else:
            print("VERDICT: fast enough to stay local.")
        return 0
    finally:
        cdb.close()
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())