"""Role-aware filler pools with distributional gating.

Two independent signals separate arguments from grammatical glue:

  * argument marking RATE: a real noun phrase head is marked by را or follows a
    preposition in a meaningful share of its own occurrences. An absolute count
    is not enough, because in a multi-million token corpus even a connective
    collects a few accidental markings.
  * sentence-level document frequency from the lexicon.

Pronouns are exempt from the frequency gate: they are legitimate arguments and
come from the closed morphological inventory.

Only corpus, lexicon, morph and syntax are opened, so this runs while the
co-occurrence build holds the usage database.
"""
from __future__ import annotations

import argparse
import math
import sqlite3
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, r"D:\Python")
sys.path.insert(0, r"D:\project\library")

from agent.config import STATE_DIR, assert_writable, ensure_dirs

from . import corpus, lexicon, morph, syntax

POOL_DB = STATE_DIR / "farsi_pool.db"

MIN_EVIDENCE = 2.0
MIN_ARG_MARKS = 3.0
MIN_ARG_RATIO = 0.02
MIN_IDF_RATIO = 0.18
MIN_TERM_LEN = 2
POSSESSIVE_PENALTY = 0.6
POSSESSIVE = ("م", "ت", "ش", "مان", "تان", "شان")

FRAGMENTS = set(morph.SAFE_PLURALS) | set(morph.VOCAB_PLURALS)
FRAGMENTS |= {e[0] for e in morph.ENDINGS} | {e[0] for e in morph.ENCLITICS}
FRAGMENTS |= {"را", "رو", "هٔ", "ۀ", "اً"}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS arg_evidence(
    term TEXT PRIMARY KEY, marked REAL, total REAL);
CREATE TABLE IF NOT EXISTS pool(
    verb_lemma TEXT, role TEXT, term TEXT, n REAL,
    idf REAL, score REAL, PRIMARY KEY(verb_lemma, role, term));
CREATE INDEX IF NOT EXISTS idx_pool_lookup
    ON pool(verb_lemma, role, score DESC);
CREATE TABLE IF NOT EXISTS meta(mkey TEXT PRIMARY KEY, mvalue TEXT);
"""


def connect(path: str | Path | None = None) -> sqlite3.Connection:
    ensure_dirs()
    target = assert_writable(Path(path) if path else POOL_DB)
    target.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(target))
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA busy_timeout=60000")
    db.executescript(_SCHEMA)
    return db


def read_meta(db: sqlite3.Connection, key: str, default: str = "0") -> str:
    row = db.execute("SELECT mvalue FROM meta WHERE mkey=?", (key,)).fetchone()
    return str(row["mvalue"]) if row else default


def write_meta(db: sqlite3.Connection, key: str, value: str) -> None:
    db.execute("INSERT INTO meta(mkey,mvalue) VALUES(?,?) "
               "ON CONFLICT(mkey) DO UPDATE SET mvalue=excluded.mvalue",
               (key, str(value)))


def scan_arguments(db: sqlite3.Connection, cdb: sqlite3.Connection,
                   max_seconds: float = 600.0) -> dict:
    started = time.time()
    marked: Counter = Counter()
    total: Counter = Counter()
    docs = 0
    for row in cdb.execute("SELECT keys FROM docs ORDER BY id"):
        words = (row["keys"] or "").split()
        docs += 1
        for index, word in enumerate(words):
            total[word] += 1
            if index + 1 < len(words) and words[index + 1] == syntax.OBJECT_MARK:
                marked[word] += 1
            if index > 0 and words[index - 1] in syntax.PREP_ROLE:
                marked[word] += 1
        if docs % 20000 == 0:
            print(f"  argument scan: docs={docs:,} terms={len(total):,} "
                  f"{round(time.time() - started, 1)}s")
        if time.time() - started > max_seconds:
            break

    db.execute("DELETE FROM arg_evidence")
    db.executemany(
        "INSERT INTO arg_evidence(term,marked,total) VALUES(?,?,?)",
        [(t, float(marked.get(t, 0)), float(n))
         for t, n in sorted(total.items())])
    write_meta(db, "arg_docs", str(docs))
    db.commit()
    return {"docs": docs, "terms": len(total),
            "seconds": round(time.time() - started, 2)}


def df_snapshot(ldb: sqlite3.Connection) -> tuple[int, dict[str, int]]:
    row = ldb.execute(
        "SELECT mvalue FROM meta WHERE mkey='sentences_total'").fetchone()
    total = int(row["mvalue"]) if row else 0
    table = {str(r["term"]): int(r["n"]) for r in
             ldb.execute("SELECT term, n FROM df")}
    return total, table


def morph_snapshot(mdb: sqlite3.Connection) -> dict[str, tuple[str, str, str]]:
    return {str(r["term"]): (str(r["pos"]), str(r["number"] or ""),
                             str(r["affixes"] or ""))
            for r in mdb.execute("SELECT term, pos, number, affixes FROM cache")}


def gate(term: str, count: float, df_total: int, df_map: dict[str, int],
         poses: dict[str, tuple[str, str, str]],
         arg: dict[str, tuple[float, float]]) -> tuple[bool, str, float]:
    if count < MIN_EVIDENCE:
        return False, "low-evidence", 0.0
    if len(term) < MIN_TERM_LEN or any(c.isdigit() for c in term):
        return False, "fragment-or-number", 0.0
    if term in syntax.PREP_ROLE or term == syntax.OBJECT_MARK or term in FRAGMENTS:
        return False, "grammatical-marker", 0.0

    pos = poses.get(term, ("UNK", "", ""))[0]
    if pos in ("V", "ADV"):
        return False, "verbal-or-adverb", 0.0

    seen = df_map.get(term, 0)
    if df_total <= 0 or seen <= 0:
        return False, "no-df", 0.0
    idf = math.log(1.0 + df_total / seen)
    ratio = idf / math.log1p(max(1, df_total))

    if pos == "PRON":
        return True, "pronoun", round(ratio, 4)

    marks, occurrences = arg.get(term, (0.0, 0.0))
    rate = marks / max(1.0, occurrences)
    if marks < MIN_ARG_MARKS or rate < MIN_ARG_RATIO:
        return False, "weak-argument-evidence", round(ratio, 4)
    if ratio < MIN_IDF_RATIO:
        return False, "too-frequent", round(ratio, 4)
    return True, "content", round(ratio, 4)


def build(db: sqlite3.Connection, sdb: sqlite3.Connection,
          ldb: sqlite3.Connection, mdb: sqlite3.Connection) -> dict:
    started = time.time()
    df_total, df_map = df_snapshot(ldb)
    if df_total <= 0:
        raise SystemExit("lexicon is empty; run the lexicon stage first")
    poses = morph_snapshot(mdb)
    arg = {str(r["term"]): (float(r["marked"]), float(r["total"]))
           for r in db.execute("SELECT term, marked, total FROM arg_evidence")}
    if not arg:
        raise SystemExit("argument evidence missing; run: python -m farsi.pool scan")

    stems = morph.known_stems(mdb)
    vocab = set(df_map)
    reasons: Counter = Counter()
    rows: list[tuple] = []

    for row in sdb.execute("SELECT verb_lemma, role, term, n FROM role_fillers"):
        term = str(row["term"])
        count = float(row["n"])
        if term not in poses:
            analysis = morph.analyze(term, stems=stems, vocab=vocab)
            poses[term] = (analysis.pos, analysis.number,
                           " ".join(analysis.affixes))
        ok, reason, ratio = gate(term, count, df_total, df_map, poses, arg)
        reasons[reason] += 1
        if not ok:
            continue
        score = math.log1p(count) * (0.6 + 0.4 * ratio)
        affixes = poses[term][2]
        if any(a in affixes for a in POSSESSIVE):
            score *= POSSESSIVE_PENALTY
        rows.append((str(row["verb_lemma"]), str(row["role"]), term,
                     count, ratio, round(score, 4)))

    db.execute("DELETE FROM pool")
    db.executemany(
        "INSERT INTO pool(verb_lemma,role,term,n,idf,score) VALUES(?,?,?,?,?,?)",
        rows)
    write_meta(db, "built_ts", str(time.time()))
    db.commit()
    return {"kept": len(rows), "reasons": dict(reasons),
            "seconds": round(time.time() - started, 2)}


def candidates(db: sqlite3.Connection, verb_lemma: str, role: str,
               limit: int = 40) -> list[dict]:
    rows = db.execute(
        "SELECT term, n, idf, score FROM pool WHERE verb_lemma=? AND role=? "
        "ORDER BY score DESC LIMIT ?",
        (verb_lemma, role, max(1, limit))).fetchall()
    return [dict(r) for r in rows]


def verbs(db: sqlite3.Connection, limit: int = 30) -> list[dict]:
    rows = db.execute(
        "SELECT verb_lemma, COUNT(*) AS n FROM pool GROUP BY verb_lemma "
        "ORDER BY n DESC LIMIT ?", (max(1, limit),)).fetchall()
    return [dict(r) for r in rows]


def stats(db: sqlite3.Connection) -> dict:
    pool_n = db.execute("SELECT COUNT(*) AS n FROM pool").fetchone()["n"]
    verbs_n = db.execute(
        "SELECT COUNT(DISTINCT verb_lemma) AS n FROM pool").fetchone()["n"]
    arg_n = db.execute("SELECT COUNT(*) AS n FROM arg_evidence").fetchone()["n"]
    return {"pool_entries": int(pool_n), "verbs": int(verbs_n),
            "arg_terms": int(arg_n), "arg_docs": int(read_meta(db, "arg_docs"))}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="farsi.pool",
                                description="استخر پالایش‌شدهٔ پرکننده‌های نقش")
    sub = p.add_subparsers(dest="command", required=True)
    sc = sub.add_parser("scan", help="شمارش نشانه‌پذیری نقش از پیکره")
    sc.add_argument("--max-seconds", type=float, default=600.0)
    sub.add_parser("build", help="ساخت استخر پالایش‌شده")
    sh = sub.add_parser("show", help="نمایش پرکننده‌های یک نقش")
    sh.add_argument("verb_lemma")
    sh.add_argument("role")
    sh.add_argument("--limit", type=int, default=20)
    vb = sub.add_parser("verbs", help="فعل‌های دارای استخر")
    vb.add_argument("--limit", type=int, default=30)
    sub.add_parser("stats", help="آمار")
    args = p.parse_args(argv)

    db = connect()
    try:
        if args.command == "scan":
            cdb = corpus.connect()
            try:
                print(scan_arguments(db, cdb, args.max_seconds))
            finally:
                cdb.close()
            print(stats(db))
            return 0
        if args.command == "build":
            sdb = syntax.connect()
            ldb = lexicon.connect()
            mdb = morph.connect()
            try:
                print(build(db, sdb, ldb, mdb))
            finally:
                mdb.close(); ldb.close(); sdb.close()
            print(stats(db))
            return 0
        if args.command == "show":
            items = candidates(db, args.verb_lemma, args.role, args.limit)
            if not items:
                print("استخری برای این فعل و نقش نیست.")
            for item in items:
                print(f"{item['term']:<20} n={item['n']:<7} "
                      f"idf={item['idf']} score={item['score']}")
            return 0
        if args.command == "verbs":
            for item in verbs(db, args.limit):
                print(f"{item['verb_lemma']:<24} {item['n']}")
            return 0
        print(stats(db))
        return 0
    except sqlite3.Error as exc:
        print("توقف:", exc)
        return 1
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())