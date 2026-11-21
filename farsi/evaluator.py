"""External, unbiased evaluation. Never trusts the model's own scores.

The model's internal fluency score is computed by the same n-gram model that
generates the sentence, so it is circular. This module evaluates against a
FROZEN test set that was created before any learning began, plus several
orthogonal signals: perplexity, repetition, semantic coherence, and whether
the sentence already exists in the corpus. None of these depend on the model's
own weights, so the model cannot fool itself.
"""
from __future__ import annotations

import argparse
import math
import sqlite3
import time
from pathlib import Path

from agent.config import STATE_DIR, assert_writable, ensure_dirs
from agent.errors import AgentError

from . import cloze, corpus, frames, lexicon, usage
from .text import sentences, tokens, normalize

EVAL_DB = STATE_DIR / "farsi_eval.db"
MIN_RELIABLE_TERMS = 200

_SCHEMA = """
CREATE TABLE IF NOT EXISTS frozen_test(
    id INTEGER PRIMARY KEY,
    keys TEXT NOT NULL UNIQUE,
    cloze_target TEXT NOT NULL,
    cloze_context TEXT NOT NULL,
    locked_ts REAL NOT NULL);
CREATE TABLE IF NOT EXISTS eval_runs(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL, kind TEXT,
    perplexity REAL, cloze_hit1 REAL, cloze_hit5 REAL,
    repetition_rate REAL, coherence REAL, items INTEGER,
    seconds REAL);
CREATE TABLE IF NOT EXISTS hallucinations(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL, text TEXT, reason TEXT, score REAL);
"""


def connect(path: str | Path | None = None) -> sqlite3.Connection:
    ensure_dirs()
    target = assert_writable(Path(path) if path else EVAL_DB)
    target.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(target))
    db.row_factory = sqlite3.Row
    db.executescript(_SCHEMA)
    return db


def lock_test_set(db: sqlite3.Connection, cdb: sqlite3.Connection,
                  udb: sqlite3.Connection, ldb: sqlite3.Connection,
                  limit: int = 500, reset: bool = False) -> dict:
    """Freeze a test set that will NEVER be used for learning.

    Once frozen, the model's performance on this set is the honest measure.
    If you re-freeze, you lose the ability to compare across time.
    """
    existing = int(db.execute(
        "SELECT COUNT(*) AS n FROM frozen_test").fetchone()["n"])
    if existing and not reset:
        return {"locked": existing, "message": "test set قفل شده؛ reset=True برای ساخت مجدد"}

    if reset:
        db.execute("DELETE FROM frozen_test")
        db.commit()

    info = lexicon.evidence(udb)
    if not info["ready"]:
        raise AgentError("پیکره هنوز برای قفل کردن test set کوچک است")

    locked = 0
    now = time.time()
    for item in cloze.items(cdb, udb, ldb, limit * cloze.TEST_EVERY):
        if item["split"] != "test":
            continue
        keys_str = " ".join(item["keys"])
        db.execute(
            "INSERT OR IGNORE INTO frozen_test"
            "(keys,cloze_target,cloze_context,locked_ts) VALUES(?,?,?,?)",
            (keys_str, item["truth"], item["context"], now),
        )
        locked += 1
        if locked >= limit:
            break
    db.commit()
    return {"locked": locked, "ts": now}


def perplexity(udb: sqlite3.Connection, text: str) -> float:
    """External perplexity, never smoothed by the model's own weights.

    Lower is better. A random guess over a 10k-word vocabulary has
    perplexity ~10000. A good Persian model should be below 100.
    """
    keys = tokens(text)
    if len(keys) < 2:
        return float("inf")
    log_sum = 0.0
    for index in range(1, len(keys)):
        prefix = " ".join(keys[max(0, index - 2):index])
        options = usage.next_options(udb, prefix, limit=50)
        prob = 0.0
        for item in options:
            token = str(item.get("token") or item.get("term") or "")
            if token == keys[index]:
                prob = float(item.get("prob") or 0.0)
                break
        if prob <= 0.0:
            row = udb.execute(
                "SELECT freq FROM terms WHERE term=?", (keys[index],)
            ).fetchone()
            total = udb.execute(
                "SELECT SUM(freq) AS s FROM terms").fetchone()["s"] or 1.0
            prob = (float(row["freq"]) if row else 0.0) / total
        log_sum += math.log(max(prob, 1e-9))
    avg_log = log_sum / (len(keys) - 1)
    return round(math.exp(-avg_log), 2)


def repetition_rate(text: str) -> float:
    """How much the sentence repeats itself. Pathological outputs repeat a lot."""
    keys = tokens(text)
    if not keys:
        return 1.0
    unique = len(set(keys))
    return round(1.0 - (unique / len(keys)), 3)


def is_in_corpus(cdb: sqlite3.Connection, text: str) -> bool:
    """Whether this is just a stored sentence disguised as a new one."""
    keys_str = " ".join(tokens(text))
    if not keys_str:
        return False
    row = cdb.execute(
        "SELECT 1 FROM docs WHERE keys=? LIMIT 1", (keys_str,)
    ).fetchone()
    return row is not None


def coherence_score(ldb: sqlite3.Connection, udb: sqlite3.Connection,
                    text: str) -> float:
    """Semantic coherence: are adjacent words actually related?

    Uses PPMI cosine between neighbors; not a learned score, just a raw
    distributional signal. 0..1 where higher is better.
    """
    keys = tokens(text)
    if len(keys) < 2:
        return 0.0
    scores = []
    for index in range(len(keys) - 1):
        try:
            peers = lexicon.related(ldb, udb, keys[index], 20)
        except (sqlite3.Error, KeyError, ValueError):
            peers = []
        match = next(
            (p for p in peers if p["term"] == keys[index + 1]), None)
        if match:
            scores.append(float(match["score"]))
        else:
            scores.append(0.0)
    return round(sum(scores) / len(scores), 4)


def detect_hallucination(cdb: sqlite3.Connection, ldb: sqlite3.Connection,
                         udb: sqlite3.Connection, text: str) -> dict:
    """Multiple signals combine to flag an output as fabricated.

    A sentence is flagged if ANY of these hold:
      - high repetition
      - very low perplexity (suspiciously fluent = memorized)
      - very high perplexity (gibberish)
      - no content words recognized by the lexicon
      - already in the corpus (just parroting)
    """
    keys = tokens(text)
    info = lexicon.evidence(udb)
    issues: list[str] = []
    score = 0.0

    if len(keys) < 3:
        issues.append("too-short")
        score += 1.0

    if info["ready"]:
        content = [k for k in keys
                   if lexicon.is_content(udb, k, docs_total=info["docs"],
                                         ready=True)]
        if not content:
            issues.append("no-content-word")
            score += 1.0

    rep = repetition_rate(text)
    if rep > 0.5:
        issues.append(f"high-repetition={rep}")
        score += 0.8

    if info["terms"] >= MIN_RELIABLE_TERMS:
        ppl = perplexity(udb, text)
        if ppl < 5.0:
            issues.append(f"suspiciously-fluent={ppl}")
            score += 0.7
        elif ppl > 500.0:
            issues.append(f"gibberish-ppl={ppl}")
            score += 0.9

    if is_in_corpus(cdb, text):
        issues.append("already-in-corpus")
        score += 0.5

    coh = coherence_score(ldb, udb, text)
    if coh < 0.05 and len(keys) > 3:
        issues.append(f"low-coherence={coh}")
        score += 0.6

    return {"text": text, "issues": issues,
            "hallucinated": score >= 1.0,
            "score": round(min(1.0, score), 3),
            "perplexity": perplexity(udb, text) if info["terms"] >= MIN_RELIABLE_TERMS else None,
            "coherence": coh, "repetition": rep,
            "in_corpus": is_in_corpus(cdb, text)}


def evaluate(db: sqlite3.Connection, cdb: sqlite3.Connection,
             udb: sqlite3.Connection, ldb: sqlite3.Connection,
             limit: int = 200) -> dict:
    """Run every reliable external metric against the frozen test set."""
    started = time.time()
    rows = db.execute(
        "SELECT * FROM frozen_test ORDER BY id LIMIT ?", (limit,)
    ).fetchall()
    if not rows:
        raise AgentError("test set قفل نشده؛ ابتدا lock-test-set را اجرا کن")

    info = lexicon.evidence(udb)
    if not info["ready"]:
        raise AgentError("پیکره هنوز کوچک است")

    hit1 = hit5 = 0
    items = 0
    for row in rows:
        truth = str(row["cloze_target"])
        context = str(row["cloze_context"])
        left, _, right = context.partition("|")
        prefix_keys = []
        raw_keys = str(row["keys"]).split()
        try:
            target_idx = raw_keys.index(truth)
            prefix_keys = raw_keys[max(0, target_idx - 2):target_idx]
        except ValueError:
            continue

        cdb_ = cloze.connect()
        try:
            ranked = cloze.predict(cdb_, udb, ldb,
                                   {"keys": raw_keys, "index": target_idx,
                                    "truth": truth, "left": left,
                                    "right": right, "context": context,
                                    "prefix": " ".join(prefix_keys),
                                    "level": 0})
        finally:
            cdb_.close()
        items += 1
        try:
            rank = ranked.index(truth) + 1
        except ValueError:
            rank = 0
        if rank == 1:
            hit1 += 1
        if 1 <= rank <= 5:
            hit5 += 1

    n = max(1, items)
    report = {
        "kind": "external-eval",
        "items": items,
        "cloze_hit1": round(100.0 * hit1 / n, 2),
        "cloze_hit5": round(100.0 * hit5 / n, 2),
        "terms": info["terms"],
        "seconds": round(time.time() - started, 2),
    }
    db.execute(
        "INSERT INTO eval_runs(ts,kind,perplexity,cloze_hit1,cloze_hit5,"
        "repetition_rate,coherence,items,seconds) VALUES(?,?,?,?,?,?,?,?,?)",
        (time.time(), report["kind"], None,
         report["cloze_hit1"], report["cloze_hit5"],
         None, None, items, report["seconds"]),
    )
    db.commit()
    return report


def stats(db: sqlite3.Connection) -> dict:
    frozen = db.execute(
        "SELECT COUNT(*) AS n FROM frozen_test").fetchone()["n"]
    runs = db.execute(
        "SELECT kind, cloze_hit1, cloze_hit5, items, seconds "
        "FROM eval_runs ORDER BY id DESC LIMIT 5").fetchall()
    return {"frozen_items": int(frozen or 0),
            "recent": [dict(r) for r in runs]}


def main(argv: list | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="farsi.evaluator",
        description="ارزیابی بی‌طرف: هرگز به نمرهٔ خود مدل اعتماد نمی‌کند",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    lock = sub.add_parser("lock-test-set",
                          help="قفل کردن test set برای همیشه")
    lock.add_argument("--limit", type=int, default=500)
    lock.add_argument("--reset", action="store_true")

    ev = sub.add_parser("evaluate",
                        help="اجرای همهٔ معیارهای خارجی روی test set قفل‌شده")
    ev.add_argument("--limit", type=int, default=200)

    hal = sub.add_parser("check",
                         help="بررسی اینکه آیا یک جمله ساختگی است")
    hal.add_argument("text")

    sub.add_parser("stats", help="آمار ارزیابی‌های قبلی")
    args = parser.parse_args(argv)

    db = connect()
    cdb = corpus.connect()
    udb = usage.connect()
    ldb = lexicon.connect()
    try:
        if args.command == "lock-test-set":
            print(lock_test_set(db, cdb, udb, ldb, args.limit, args.reset))
            return 0
        if args.command == "evaluate":
            print(evaluate(db, cdb, udb, ldb, args.limit))
            return 0
        if args.command == "check":
            report = detect_hallucination(cdb, ldb, udb, args.text)
            print(f"جمله: {report['text']}")
            print(f"ساختگی: {'بله' if report['hallucinated'] else 'خیر'}")
            print(f"امتیاز مشکوک بودن: {report['score']}")
            print(f"perplexity: {report['perplexity']}")
            print(f"coherence: {report['coherence']}")
            print(f"تکرار: {report['repetition']}")
            print(f"در corpus: {report['in_corpus']}")
            if report["issues"]:
                print(f"مشکلات: {', '.join(report['issues'])}")
            return 0
        print(stats(db))
        return 0
    except (AgentError, OSError, ValueError, sqlite3.Error) as exc:
        print("توقف:", exc)
        return 1
    finally:
        ldb.close(); udb.close(); cdb.close(); db.close()


if __name__ == "__main__":
    raise SystemExit(main())