"""Cloze drills mined from the corpus, with the corpus as its own teacher.
A real sentence loses one content word; the model must guess it from the
visible neighbours only. The hidden word is never used to build candidates,
so grading is honest. Items are split deterministically into train and test:
learning touches train only, and the test score is the number that tells you
whether more text actually helped. Feedback stays context-local (left|right
slot evidence) in this module's own database, so global word weights are not
inflated and no finished sentence is ever stored.
"""
from __future__ import annotations

import argparse
import sqlite3
import time
import zlib
from pathlib import Path

from agent.config import STATE_DIR, assert_writable, ensure_dirs
from agent.errors import AgentError

from . import corpus, lexicon, usage
from .text import sentences, tokens

CLOZE_DB = STATE_DIR / "farsi_cloze.db"
MIN_TOKENS = 6
MAX_TOKENS = 20
MAX_CANDIDATES = 40
TEST_EVERY = 5
W_NEXT = 1.0
W_LEFT = 0.45
W_RIGHT = 0.35

_SCHEMA = """
CREATE TABLE IF NOT EXISTS slot_evidence(
context TEXT, term TEXT,
hits INTEGER DEFAULT 0, misses INTEGER DEFAULT 0,
updated_ts REAL, PRIMARY KEY(context, term));
CREATE TABLE IF NOT EXISTS runs(
id INTEGER PRIMARY KEY AUTOINCREMENT,
ts REAL, kind TEXT, items INTEGER,
hit1 REAL, hit5 REAL, hit10 REAL, mrr REAL, seconds REAL);
CREATE TABLE IF NOT EXISTS bench(
id INTEGER PRIMARY KEY AUTOINCREMENT,
keys TEXT NOT NULL, idx INTEGER NOT NULL, truth TEXT NOT NULL,
left TEXT NOT NULL, right TEXT NOT NULL, context TEXT NOT NULL,
prefix TEXT NOT NULL, level INTEGER NOT NULL,
UNIQUE(keys, idx));
"""


def connect(path: str | Path | None = None) -> sqlite3.Connection:
    ensure_dirs()
    target = assert_writable(Path(path) if path else CLOZE_DB)
    target.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(target))
    db.row_factory = sqlite3.Row
    db.executescript(_SCHEMA)
    return db


def _ctx_weight(db: sqlite3.Connection, context: str, term: str) -> float:
    row = db.execute(
        "SELECT hits, misses FROM slot_evidence WHERE context=? AND term=?",
        (context, term)).fetchone()
    if row is None:
        return 1.0
    hits = int(row["hits"] or 0)
    misses = int(row["misses"] or 0)
    return max(0.2, 2.0 * (hits + 1) / (hits + misses + 2))


def _bump(db: sqlite3.Connection, context: str, term: str,
          hit: bool) -> None:
    db.execute(
        "INSERT INTO slot_evidence(context,term,hits,misses,updated_ts) "
        "VALUES(?,?,?,?,?) "
        "ON CONFLICT(context,term) DO UPDATE SET hits=hits+?, misses=misses+?, "
        "updated_ts=excluded.updated_ts",
        (context, term, 1 if hit else 0, 0 if hit else 1, time.time(),
         1 if hit else 0, 0 if hit else 1),
    )


def _norm_options(udb: sqlite3.Connection, prefix: str,
                  limit: int) -> list[tuple[str, float]]:
    try:
        raw = usage.next_options(udb, prefix, limit=limit)
    except (sqlite3.Error, KeyError, ValueError, TypeError):
        return []
    out: list[tuple[str, float]] = []
    for item in raw or []:
        if isinstance(item, dict):
            term = str(item.get("term") or item.get("word")
                       or item.get("token") or "")
            score = float(item.get("score") or item.get("p")
                          or item.get("prob") or 0.0)
        elif isinstance(item, (tuple, list)) and len(item) >= 2:
            term, score = str(item[0]), float(item[1])
        else:
            term, score = str(item), 1.0
        if term:
            out.append((term, score))
    return out


def _probs(items: list[tuple[str, float]]) -> dict[str, float]:
    if not items:
        return {}
    values = [max(0.0, s) for _, s in items]
    if max(values) <= 0.0:
        values = [1.0 for _ in items]
    total = sum(values) or 1.0
    return {t: v / total for (t, _), v in zip(items, values)}


def items(cdb, udb, ldb, limit: int, level: int = 0):
    """Yield cloze items; difficulty comes from frequency and context depth."""
    info = lexicon.evidence(udb)
    if not info["ready"]:
        return
    docs_total = info["docs"]
    cache: dict[str, bool] = {}
    made = 0
    for row in cdb.execute("SELECT id, raw FROM docs ORDER BY id"):
        for sentence in sentences(row["raw"] or ""):
            keys = tokens(sentence)
            if not MIN_TOKENS <= len(keys) <= MAX_TOKENS:
                continue
            for index in range(1, len(keys) - 1):
                truth = keys[index]
                if truth not in cache:
                    cache[truth] = lexicon.is_content(
                        udb, truth, docs_total=docs_total, ready=True)
                if not cache[truth]:
                    continue
                term_row = usage.term(udb, truth)
                freq = float(term_row["freq"]) if term_row else 0.0
                item_level = 1 if freq >= 20.0 else (2 if freq >= 5.0 else 3)
                if level and item_level != level:
                    continue
                left, right = keys[index - 1], keys[index + 1]
                yield {
                    "keys": keys, "index": index, "truth": truth,
                    "left": left, "right": right,
                    "context": left + "|" + right,
                    "prefix": " ".join(keys[max(0, index - 2):index]),
                    "level": item_level,
                    "split": ("test" if zlib.crc32(
                        " ".join(keys).encode("utf-8")) % TEST_EVERY == 0
                        else "train"),
                }
                made += 1
                if made >= limit:
                    return


def predict(db, udb, ldb, item: dict) -> list[str]:
    """Candidates come only from visible words; the answer is never an input."""
    scores: dict[str, float] = {}
    for term, p in _probs(_norm_options(udb, item["prefix"], 12)).items():
        scores[term] = scores.get(term, 0.0) + W_NEXT * p
    for side, weight in ((item["left"], W_LEFT), (item["right"], W_RIGHT)):
        try:
            peers = lexicon.related(ldb, udb, side, 8)
        except (sqlite3.Error, KeyError, ValueError):
            peers = []
        for peer in peers:
            term = str(peer["term"])
            if " " in term:
                continue
            scores[term] = (scores.get(term, 0.0)
                            + weight * float(peer["score"]))
    scores.pop(item["truth"], None) if False else None
    ranked = []
    for term, value in scores.items():
        if not term or term == item["left"] or term == item["right"]:
            continue
        value *= lexicon.word_weight(ldb, term)
        value *= _ctx_weight(db, item["context"], term)
        ranked.append((term, value))
    ranked.sort(key=lambda pair: (-pair[1], pair[0]))
    return [term for term, _ in ranked[:MAX_CANDIDATES]]


def _rank_of(ranked: list[str], truth: str) -> int:
    try:
        return ranked.index(truth) + 1
    except ValueError:
        return 0


def bench_items(db) -> list[dict]:
    rows = db.execute("SELECT * FROM bench ORDER BY id").fetchall()
    return [{"keys": str(row["keys"]).split(), "index": int(row["idx"]),
             "truth": str(row["truth"]), "left": str(row["left"]),
             "right": str(row["right"]), "context": str(row["context"]),
             "prefix": str(row["prefix"]), "level": int(row["level"]),
             "split": "test"} for row in rows]


def freeze(db, cdb, udb, ldb, limit: int = 200,
           reset: bool = False) -> dict:
    """Store the test items once, so later exams use the same ruler.

    Which positions qualify as content words depends on corpus-wide idf and
    frequency, so adding text silently changes a live test set. Freezing
    removes that confound. Training never reads the frozen set.
    """
    existing = int(db.execute("SELECT COUNT(*) AS n FROM bench").fetchone()["n"])
    if existing and not reset:
        return {"frozen": existing, "added": 0,
                "message": "بنچمارک موجود است؛ برای ساخت مجدد reset=True"}
    if reset:
        db.execute("DELETE FROM bench")
        db.commit()
    for item in items(cdb, udb, ldb, limit * TEST_EVERY):
        if item["split"] != "test":
            continue
        db.execute(
            "INSERT OR IGNORE INTO bench"
            "(keys,idx,truth,left,right,context,prefix,level) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (" ".join(item["keys"]), item["index"], item["truth"],
             item["left"], item["right"], item["context"], item["prefix"],
             item["level"]))
        if int(db.execute("SELECT COUNT(*) AS n FROM bench").fetchone()["n"]) >= limit:
            break
    db.commit()
    total = int(db.execute("SELECT COUNT(*) AS n FROM bench").fetchone()["n"])
    return {"frozen": total, "added": total if reset else total - existing}


def exam(db, cdb, udb, ldb, limit: int = 200, level: int = 0,
         split: str = "test", learn: bool = False) -> dict:
    started = time.time()
    tally = {"items": 0, "hit1": 0, "hit5": 0, "hit10": 0, "mrr": 0.0}
    per_level: dict[int, dict] = {}
    frozen = [] if learn else bench_items(db)
    source = "frozen" if frozen else "live"
    if frozen:
        stream = [it for it in frozen if not level or it["level"] == level]
    else:
        stream = (it for it in items(cdb, udb, ldb, limit * TEST_EVERY, level)
                  if not split or it["split"] == split)
    for item in stream:
        ranked = predict(db, udb, ldb, item)
        rank = _rank_of(ranked, item["truth"])
        bucket = per_level.setdefault(item["level"], {"n": 0, "hit5": 0})
        bucket["n"] += 1
        tally["items"] += 1
        if rank:
            tally["mrr"] += 1.0 / rank
            if rank <= 1:
                tally["hit1"] += 1
            if rank <= 5:
                tally["hit5"] += 1
                bucket["hit5"] += 1
            if rank <= 10:
                tally["hit10"] += 1
        if learn and item["split"] == "train":
            _bump(db, item["context"], item["truth"], True)
            for wrong in ranked[:3]:
                if wrong != item["truth"]:
                    _bump(db, item["context"], wrong, False)
        if tally["items"] >= limit:
            break
    if learn:
        db.commit()
    n = max(1, tally["items"])
    report = {
        "kind": "train" if learn else "exam", "split": split,
        "source": source, "items": tally["items"],
        "hit1": round(100.0 * tally["hit1"] / n, 1),
        "hit5": round(100.0 * tally["hit5"] / n, 1),
        "hit10": round(100.0 * tally["hit10"] / n, 1),
        "mrr": round(tally["mrr"] / n, 4),
        "levels": {k: {"n": v["n"],
                       "hit5": round(100.0 * v["hit5"] / max(1, v["n"]), 1)}
                   for k, v in sorted(per_level.items())},
        "seconds": round(time.time() - started, 2),
    }
    db.execute(
        "INSERT INTO runs(ts,kind,items,hit1,hit5,hit10,mrr,seconds) "
        "VALUES(?,?,?,?,?,?,?,?)",
        (time.time(), report["kind"], report["items"], report["hit1"],
         report["hit5"], report["hit10"], report["mrr"], report["seconds"]))
    db.commit()
    return report


def show(db, cdb, udb, ldb, limit: int = 5) -> list[dict]:
    out = []
    for item in items(cdb, udb, ldb, limit * TEST_EVERY):
        ranked = predict(db, udb, ldb, item)
        masked = list(item["keys"])
        masked[item["index"]] = "____"
        out.append({
            "sentence": " ".join(lexicon.surface_for(ldb, k) for k in masked),
            "truth": lexicon.surface_for(ldb, item["truth"]),
            "guesses": [lexicon.surface_for(ldb, t) for t in ranked[:5]],
            "rank": _rank_of(ranked, item["truth"]),
            "level": item["level"], "split": item["split"],
        })
        if len(out) >= limit:
            break
    return out


def stats(db) -> dict:
    rows = db.execute("SELECT COUNT(*) AS n FROM slot_evidence").fetchone()
    runs = db.execute(
        "SELECT kind, items, hit1, hit5, hit10, mrr FROM runs "
        "ORDER BY id DESC LIMIT 5").fetchall()
    return {"contexts": int(rows["n"] or 0),
            "recent": [dict(r) for r in runs]}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="farsi.cloze", description="تمرین جای‌خالی با تصحیح خودکار")
    sub = parser.add_subparsers(dest="command", required=True)
    ex = sub.add_parser("exam", help="آزمون روی دادهٔ کنار گذاشته‌شده")
    ex.add_argument("--limit", type=int, default=200)
    ex.add_argument("--level", type=int, default=0)
    ex.add_argument("--split", default="test", choices=("test", "train", ""))
    tr = sub.add_parser("train", help="یادگیری از تمرین‌های آموزشی")
    tr.add_argument("--limit", type=int, default=1000)
    tr.add_argument("--level", type=int, default=0)
    sh = sub.add_parser("show", help="چند نمونه با حدس مدل")
    sh.add_argument("--limit", type=int, default=5)
    sub.add_parser("stats", help="آمار")
    args = parser.parse_args(argv)
    db = connect()
    cdb = corpus.connect()
    udb = usage.connect()
    ldb = lexicon.connect()
    try:
        if args.command in ("exam", "train"):
            report = exam(db, cdb, udb, ldb, args.limit, args.level,
                          "train" if args.command == "train" else args.split,
                          learn=args.command == "train")
            for key in ("kind", "split", "source", "items", "hit1", "hit5",
                        "hit10", "mrr", "levels", "seconds"):
                print(f"{key}: {report[key]}")
            return 0
        if args.command == "show":
            for item in show(db, cdb, udb, ldb, args.limit):
                print("\nجمله:", item["sentence"])
                print("درست:", item["truth"], "| رتبه:", item["rank"] or "نیست",
                      "| سطح:", item["level"], "|", item["split"])
                print("حدس‌ها:", " ، ".join(item["guesses"]))
            return 0
        print(stats(db))
        return 0
    except (AgentError, OSError, ValueError, sqlite3.Error) as exc:
        print("توقف:", exc)
        return 1
    finally:
        ldb.close()
        udb.close()
        cdb.close()
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())