"""Usage statistics: co-occurrence, phrase discovery, neighbours and n-grams.

Counts are updated incrementally, so more text simply adds evidence. The n-gram
table is a ranker for candidate sentences, never a free generator: word-level
Markov cannot enforce Persian grammar.
"""
from __future__ import annotations

import argparse
import json
import math
import time
from collections import Counter
from pathlib import Path

from agent.config import STATE_DIR, assert_writable, ensure_dirs, get_logger

from . import corpus
from .text import tokens

log = get_logger("farsi.usage")

USAGE_DB = STATE_DIR / "farsi_usage.db"
CONFIDENCE_TARGET = 30.0
SMOOTHING = 0.1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS terms(
term TEXT PRIMARY KEY, freq REAL, docs INTEGER, updated_ts REAL);
CREATE TABLE IF NOT EXISTS pairs(a TEXT, b TEXT, n REAL, PRIMARY KEY(a,b));
CREATE INDEX IF NOT EXISTS idx_pairs_a ON pairs(a, n DESC);
CREATE INDEX IF NOT EXISTS idx_pairs_b ON pairs(b, n DESC);
CREATE TABLE IF NOT EXISTS grams(prev TEXT, nxt TEXT, n REAL, PRIMARY KEY(prev,nxt));
CREATE INDEX IF NOT EXISTS idx_grams_prev ON grams(prev, n DESC);
CREATE TABLE IF NOT EXISTS phrases(
phrase TEXT PRIMARY KEY, n REAL, npmi REAL, updated_ts REAL);
CREATE TABLE IF NOT EXISTS meta(mkey TEXT PRIMARY KEY, mvalue TEXT);
"""


def connect(path: str | Path | None = None):
    import sqlite3

    ensure_dirs()
    target = assert_writable(Path(path) if path else USAGE_DB)
    target.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(target))
    db.row_factory = sqlite3.Row
    db.executescript(_SCHEMA)
    return db


def _meta(db, key: str, default):
    row = db.execute("SELECT mvalue FROM meta WHERE mkey=?", (key,)).fetchone()
    return json.loads(row["mvalue"]) if row else default


def _set_meta(db, key: str, value) -> None:
    db.execute(
        "INSERT INTO meta(mkey,mvalue) VALUES(?,?) "
        "ON CONFLICT(mkey) DO UPDATE SET mvalue=excluded.mvalue",
        (key, json.dumps(value, ensure_ascii=False)),
    )


def _flush(db, terms: Counter, docfreq: Counter, pairs: Counter, grams: Counter) -> None:
    now = time.time()
    db.executemany(
        "INSERT INTO terms(term,freq,docs,updated_ts) VALUES(?,?,?,?) "
        "ON CONFLICT(term) DO UPDATE SET freq=freq+excluded.freq, "
        "docs=docs+excluded.docs, updated_ts=excluded.updated_ts",
        [(t, n, docfreq[t], now) for t, n in terms.items()],
    )
    db.executemany(
        "INSERT INTO pairs(a,b,n) VALUES(?,?,?) "
        "ON CONFLICT(a,b) DO UPDATE SET n=n+excluded.n",
        [(a, b, n) for (a, b), n in pairs.items()],
    )
    db.executemany(
        "INSERT INTO grams(prev,nxt,n) VALUES(?,?,?) "
        "ON CONFLICT(prev,nxt) DO UPDATE SET n=n+excluded.n",
        [(p, x, n) for (p, x), n in grams.items()],
    )
    db.commit()


def build(db, corpus_db, *, window: int = 4, limit_docs: int | None = None,
          max_seconds: float = 120.0) -> dict:
    """Absorb only documents that were added since the previous run."""
    if not corpus.learning_enabled():
        return {"skipped": "learn-off", "docs": 0}

    started = time.time()
    last = int(_meta(db, "last_doc", 0))
    totals = _meta(db, "totals", {"tokens": 0.0, "bigrams": 0.0,
                                  "trigrams": 0.0, "windows": 0.0})
    terms: Counter = Counter()
    docfreq: Counter = Counter()
    pairs: Counter = Counter()
    grams: Counter = Counter()
    docs = seen_tokens = 0
    newest = last

    cursor = corpus_db.execute(
        "SELECT id, keys, quality, weight FROM docs WHERE id > ? ORDER BY id", (last,)
    )
    for row in cursor:
        words = (row["keys"] or "").split()
        newest = max(newest, int(row["id"]))
        if not words:
            continue
        # Low-quality text still counts, only with less influence.
        weight = float(row["weight"] or 1.0) * max(0.2, float(row["quality"] or 0.5))
        docs += 1
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

        if docs % 200 == 0:
            _flush(db, terms, docfreq, pairs, grams)
            terms.clear(); docfreq.clear(); pairs.clear(); grams.clear()
        if limit_docs and docs >= limit_docs:
            break
        if time.time() - started > max_seconds:
            break

    _flush(db, terms, docfreq, pairs, grams)
    _set_meta(db, "totals", totals)
    _set_meta(db, "last_doc", newest)
    db.commit()
    return {"docs": docs, "tokens": seen_tokens,
            "seconds": round(time.time() - started, 2)}


def term(db, word: str) -> dict | None:
    key = (tokens(word) or [""])[0]
    row = db.execute("SELECT * FROM terms WHERE term=?", (key,)).fetchone()
    return dict(row) if row else None


def confidence(db, word: str) -> float:
    row = term(db, word)
    if row is None:
        return 0.0
    return round(min(1.0, math.log1p(row["freq"]) / math.log1p(CONFIDENCE_TARGET)), 3)


def _vector(db, word: str, size: int, totals: dict) -> dict[str, float]:
    """PPMI weights over the strongest contexts of one term."""
    freq = term(db, word)
    if freq is None or totals["tokens"] <= 0 or totals["windows"] <= 0:
        return {}
    rows = db.execute(
        "SELECT b, n FROM pairs WHERE a=? ORDER BY n DESC LIMIT ?", (word, size)
    ).fetchall()
    vector: dict[str, float] = {}
    for row in rows:
        context = term(db, row["b"])
        if context is None or context["freq"] <= 0:
            continue
        joint = row["n"] / totals["windows"]
        marginal = (freq["freq"] / totals["tokens"]) * (context["freq"] / totals["tokens"])
        if joint <= 0 or marginal <= 0:
            continue
        weight = math.log2(joint / marginal)
        if weight > 0:
            vector[row["b"]] = weight
    return vector


def neighbors(db, word: str, limit: int = 10, size: int = 48) -> list[dict]:
    """Cosine over shared contexts; cheap and deterministic, not a full SVD."""
    key = (tokens(word) or [""])[0]
    totals = _meta(db, "totals", {})
    base = _vector(db, key, size, totals)
    if not base:
        return []
    candidates: dict[str, dict[str, float]] = {}
    for context in base:
        rows = db.execute(
            "SELECT a, n FROM pairs WHERE b=? ORDER BY n DESC LIMIT ?", (context, size)
        ).fetchall()
        for row in rows:
            if row["a"] == key:
                continue
            other = term(db, row["a"])
            if other is None or other["freq"] <= 0:
                continue
            joint = row["n"] / max(1e-9, totals.get("windows", 0.0))
            marginal = (other["freq"] / totals["tokens"]) * (
                term(db, context)["freq"] / totals["tokens"])
            if joint <= 0 or marginal <= 0:
                continue
            weight = math.log2(joint / marginal)
            if weight > 0:
                candidates.setdefault(row["a"], {})[context] = weight

    base_norm = math.sqrt(sum(v * v for v in base.values())) or 1.0
    scored = []
    for name, vector in candidates.items():
        shared = set(base) & set(vector)
        if not shared:
            continue
        dot = sum(base[c] * vector[c] for c in shared)
        other_norm = math.sqrt(sum(vector[c] ** 2 for c in shared)) or 1.0
        score = dot / (base_norm * other_norm)
        scored.append({"term": name, "score": round(score, 4), "shared": len(shared),
                       "confidence": confidence(db, name)})
    scored.sort(key=lambda item: (-item["score"], -item["shared"], item["term"]))
    return scored[:limit]


def discover_phrases(db, *, min_count: float = 3.0, min_npmi: float = 0.35,
                     limit: int = 40) -> list[dict]:
    """Surprising n-grams become single units, so rare idioms need few samples."""
    totals = _meta(db, "totals", {})
    total_tokens = totals.get("tokens", 0.0)
    if total_tokens <= 0:
        return []
    freq = {row["term"]: row["freq"]
            for row in db.execute("SELECT term, freq FROM terms")}
    found: list[tuple[str, float, float]] = []
    for row in db.execute("SELECT prev, nxt, n FROM grams WHERE n >= ?", (min_count,)):
        parts = row["prev"].split()
        pieces = parts + [row["nxt"]]
        if len(pieces) == 2:
            total = totals.get("bigrams", 0.0)
        elif len(pieces) == 3:
            total = totals.get("trigrams", 0.0)
        else:
            continue
        if total <= 0 or any(freq.get(p, 0.0) < 2 for p in pieces):
            continue
        joint = row["n"] / total
        marginal = 1.0
        for piece in pieces:
            marginal *= freq[piece] / total_tokens
        if joint <= 0 or marginal <= 0:
            continue
        npmi = math.log2(joint / marginal) / -math.log2(joint)
        npmi = max(-1.0, min(1.0, npmi))
        if npmi >= min_npmi:
            found.append((" ".join(pieces), row["n"], npmi))

    now = time.time()
    db.executemany(
        "INSERT INTO phrases(phrase,n,npmi,updated_ts) VALUES(?,?,?,?) "
        "ON CONFLICT(phrase) DO UPDATE SET n=excluded.n, npmi=excluded.npmi, "
        "updated_ts=excluded.updated_ts",
        [(p, n, s, now) for p, n, s in found],
    )
    db.commit()
    rows = db.execute(
        "SELECT phrase, n, npmi FROM phrases ORDER BY npmi DESC, n DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [dict(row) for row in rows]


def _options(db, prev: str) -> list[dict]:
    rows = db.execute(
        "SELECT nxt, n FROM grams WHERE prev=? ORDER BY n DESC", (prev,)
    ).fetchall()
    total = sum(row["n"] for row in rows)
    if total <= 0:
        return []
    return [{"token": row["nxt"], "prob": row["n"] / total} for row in rows]


def next_options(db, prefix: str, limit: int = 5) -> list[dict]:
    words = tokens(prefix)
    for depth in (2, 1):
        if len(words) >= depth:
            options = _options(db, " ".join(words[-depth:]))
            if options:
                for item in options:
                    item["backoff"] = depth
                return options[:limit]
    rows = db.execute(
        "SELECT term, freq FROM terms ORDER BY freq DESC LIMIT ?", (limit,)
    ).fetchall()
    total = sum(row["freq"] for row in rows) or 1.0
    return [{"token": row["term"], "prob": row["freq"] / total, "backoff": 0}
            for row in rows]


def score(db, text: str) -> float:
    """Mean log2 probability with backoff. Compare candidates; higher is better."""
    words = tokens(text)
    if len(words) < 2:
        return 0.0
    vocab = db.execute("SELECT COUNT(*) AS n FROM terms").fetchone()["n"] or 1
    total_freq = db.execute("SELECT SUM(freq) AS s FROM terms").fetchone()["s"] or 1.0
    values = []
    for index in range(1, len(words)):
        probability = None
        for depth in (2, 1):
            if index >= depth:
                prev = " ".join(words[index - depth:index])
                row = db.execute(
                    "SELECT n FROM grams WHERE prev=? AND nxt=?", (prev, words[index])
                ).fetchone()
                total = db.execute(
                    "SELECT SUM(n) AS s FROM grams WHERE prev=?", (prev,)
                ).fetchone()["s"]
                if row and total:
                    probability = (row["n"] + SMOOTHING) / (total + SMOOTHING * vocab)
                    break
        if probability is None:
            row = db.execute(
                "SELECT freq FROM terms WHERE term=?", (words[index],)
            ).fetchone()
            probability = ((row["freq"] if row else 0.0) + SMOOTHING) / (
                total_freq + SMOOTHING * vocab)
        values.append(math.log2(probability))
    return round(sum(values) / len(values), 4)


def prune(db, min_pair: float = 2.0, keep_contexts: int = 64) -> dict:
    before = db.execute("SELECT COUNT(*) AS n FROM pairs").fetchone()["n"]
    db.execute("DELETE FROM pairs WHERE n < ?", (min_pair,))
    db.execute(
        "DELETE FROM pairs WHERE rowid IN (SELECT rowid FROM ("
        "SELECT rowid, ROW_NUMBER() OVER (PARTITION BY a ORDER BY n DESC) AS rn "
        "FROM pairs) WHERE rn > ?)", (keep_contexts,),
    )
    db.execute("DELETE FROM grams WHERE n < ?", (min_pair,))
    db.commit()
    after = db.execute("SELECT COUNT(*) AS n FROM pairs").fetchone()["n"]
    return {"pairs_before": before, "pairs_after": after}


def stats(db) -> dict:
    def count(table):
        return db.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]

    return {"terms": count("terms"), "pairs": count("pairs"),
            "grams": count("grams"), "phrases": count("phrases"),
            "last_doc": int(_meta(db, "last_doc", 0)),
            "totals": _meta(db, "totals", {})}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="farsi.usage", description="آمار هم‌نشینی")
    sub = parser.add_subparsers(dest="command", required=True)
    build_parser = sub.add_parser("build", help="به‌روزرسانی از پیکره")
    build_parser.add_argument("--window", type=int, default=4)
    build_parser.add_argument("--limit-docs", type=int, default=None)
    near = sub.add_parser("neighbors", help="واژه‌های نزدیک")
    near.add_argument("word")
    near.add_argument("--limit", type=int, default=10)
    phr = sub.add_parser("phrases", help="کشف عبارت‌ها")
    phr.add_argument("--min-count", type=float, default=3.0)
    phr.add_argument("--min-npmi", type=float, default=0.35)
    nxt = sub.add_parser("next", help="واژهٔ بعدی محتمل")
    nxt.add_argument("prefix")
    sc = sub.add_parser("score", help="امتیاز روانی متن")
    sc.add_argument("text")
    sub.add_parser("stats", help="آمار جدول‌ها")
    sub.add_parser("prune", help="هرس جدول‌ها")
    args = parser.parse_args(argv)

    db = connect()
    try:
        if args.command == "build":
            cdb = corpus.connect()
            try:
                print(build(db, cdb, window=args.window, limit_docs=args.limit_docs))
            finally:
                cdb.close()
            discover_phrases(db)
            print(stats(db))
        elif args.command == "neighbors":
            for item in neighbors(db, args.word, args.limit):
                print(f"{item['term']:20} {item['score']:.3f} "
                      f"shared={item['shared']} conf={item['confidence']}")
        elif args.command == "phrases":
            for item in discover_phrases(db, min_count=args.min_count,
                                         min_npmi=args.min_npmi):
                print(f"{item['phrase']:34} n={item['n']:.1f} npmi={item['npmi']:.3f}")
        elif args.command == "next":
            for item in next_options(db, args.prefix):
                print(f"{item['token']:20} {item['prob']:.3f} backoff={item['backoff']}")
        elif args.command == "score":
            print("score:", score(db, args.text))
        elif args.command == "prune":
            print(prune(db))
        else:
            print(stats(db))
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())