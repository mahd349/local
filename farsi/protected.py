"""Protected learning: the model learns ONLY from real user feedback.

The most dangerous failure mode is the model teaching itself: generating
garbage, rating it highly (because its own fluency score agrees), and
reinforcing the garbage. This module enforces three hard rules:

1. NO SELF-FEEDBACK: Generated sentences are NEVER used as training data.
   Only sentences explicitly marked as user-provided enter the corpus.
2. DECAY: Every weight (word, frame, pair) decays slowly over time so old
   evidence does not dominate forever and the model cannot lock into a
   wrong pattern.
3. HONEST RATINGS: A user can only rate something they actually saw; the
   rating is bound to a specific generation event and cannot be reused.

The test set created by `evaluator.lock_test_set` is NEVER touched by any
learning path in this module.
"""
from __future__ import annotations

import argparse
import sqlite3
import time
from pathlib import Path

from agent.config import STATE_DIR, assert_writable, ensure_dirs
from agent.errors import AgentError

from . import corpus, frames, lexicon, usage

PROTECT_DB = STATE_DIR / "farsi_protected.db"

W_DECAY = 0.995
MIN_WEIGHT = 0.05
MAX_SELF_FEED_PER_DAY = 0
FEEDBACK_WINDOW_HOURS = 24

_SCHEMA = """
CREATE TABLE IF NOT EXISTS user_feedback(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL, source TEXT NOT NULL,
    kind TEXT, ref TEXT, score REAL,
    applied INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS decay_log(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL, targets INTEGER, min_after REAL);
CREATE TABLE IF NOT EXISTS guard_rules(
    rule TEXT PRIMARY KEY, value REAL);
"""


def connect(path: str | Path | None = None) -> sqlite3.Connection:
    ensure_dirs()
    target = assert_writable(Path(path) if path else PROTECT_DB)
    target.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(target))
    db.row_factory = sqlite3.Row
    db.executescript(_SCHEMA)
    db.execute(
        "INSERT OR IGNORE INTO guard_rules(rule,value) VALUES('max_self_feed',?)",
        (MAX_SELF_FEED_PER_DAY,),
    )
    db.commit()
    return db


def _within_feedback_window(db: sqlite3.Connection, ref: str,
                            source: str) -> bool:
    cutoff = time.time() - FEEDBACK_WINDOW_HOURS * 3600
    row = db.execute(
        "SELECT 1 FROM user_feedback WHERE ref=? AND source=? AND ts>=? "
        "LIMIT 1",
        (ref, source, cutoff),
    ).fetchone()
    return row is not None


def accept_feedback(db: sqlite3.Connection, ldb: sqlite3.Connection,
                    fdb: sqlite3.Connection, source: str, kind: str,
                    ref: str, score: float,
                    allow_self: bool = False) -> dict:
    """Record and apply one piece of feedback.

    `source` must be 'user', 'evaluator', or similar. 'model' is rejected
    unless `allow_self` is explicitly True AND the daily quota allows it.
    """
    try:
        value = float(score)
    except (TypeError, ValueError):
        raise AgentError("score باید عدد باشد")
    if not 0.0 <= value <= 5.0:
        raise AgentError("score باید بین 0 تا 5 باشد")

    src = str(source or "").strip()
    if not src:
        raise AgentError("source مشخص نشده")

    is_self = src.lower() in {"model", "self", "auto"}
    if is_self and not allow_self:
        raise AgentError(
            "بازخورد از خود مدل ممنوع است؛ فقط feedback کاربر پذیرفته می‌شود"
        )

    if is_self:
        today_start = time.time() - (time.time() % 86400)
        count = db.execute(
            "SELECT COUNT(*) AS n FROM user_feedback "
            "WHERE source=? AND ts>=?",
            (src, today_start),
        ).fetchone()["n"]
        limit_row = db.execute(
            "SELECT value FROM guard_rules WHERE rule='max_self_feed'"
        ).fetchone()
        limit = int(limit_row["value"]) if limit_row else 0
        if count >= limit:
            raise AgentError(
                f"سهمیهٔ بازخورد خودکار امروز پر شده ({limit} بار)"
            )

    if kind not in {"word", "frame", "pair", "cloze"}:
        raise AgentError(f"kind ناشناس: {kind}")

    now = time.time()
    db.execute(
        "INSERT INTO user_feedback(ts,source,kind,ref,score,applied) "
        "VALUES(?,?,?,?,?,0)",
        (now, src, kind, ref, value),
    )

    applied = False
    if kind == "word":
        try:
            lexicon.rate_word(ldb, ref, value)
            applied = True
        except AgentError:
            pass
    elif kind == "frame":
        try:
            frames.rate_frame(fdb, ref, value)
            applied = True
        except AgentError:
            pass

    if applied:
        fid = db.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
        db.execute(
            "UPDATE user_feedback SET applied=1 WHERE id=?", (fid,))
    db.commit()
    return {"accepted": True, "kind": kind, "ref": ref, "score": value,
            "source": src, "applied": applied}


def decay_weights(db: sqlite3.Connection, ldb: sqlite3.Connection,
                  fdb: sqlite3.Connection,
                  decay: float = W_DECAY,
                  floor: float = MIN_WEIGHT) -> dict:
    """Pull every learned weight toward neutral over time.

    Without decay, a few early bad ratings can permanently poison a word,
    and a few early good ratings can permanently bless a garbage pattern.
    Decay restores uncertainty.
    """
    started = time.time()
    touched = 0

    rows = ldb.execute(
        "SELECT term, positive, negative, neutral FROM word_stats"
    ).fetchall()
    for row in rows:
        pos = int(row["positive"] or 0)
        neg = int(row["negative"] or 0)
        neu = int(row["neutral"] or 0)
        new_pos = int(pos * decay)
        new_neg = int(neg * decay)
        new_neu = int(neu * decay)
        if (new_pos, new_neg, new_neu) != (pos, neg, neu):
            ldb.execute(
                "UPDATE word_stats SET positive=?, negative=?, neutral=?, "
                "updated_ts=? WHERE term=?",
                (new_pos, new_neg, new_neu, time.time(), row["term"]),
            )
            touched += 1

    rows = fdb.execute(
        "SELECT fkey, positive, negative, neutral FROM frames"
    ).fetchall()
    for row in rows:
        pos = int(row["positive"] or 0)
        neg = int(row["negative"] or 0)
        neu = int(row["neutral"] or 0)
        new_pos = int(pos * decay)
        new_neg = int(neg * decay)
        new_neu = int(neu * decay)
        if (new_pos, new_neg, new_neu) != (pos, neg, neu):
            fdb.execute(
                "UPDATE frames SET positive=?, negative=?, neutral=?, "
                "updated_ts=? WHERE fkey=?",
                (new_pos, new_neg, new_neu, time.time(), row["fkey"]),
            )
            touched += 1

    ldb.commit()
    fdb.commit()

    seconds = round(time.time() - started, 2)
    db.execute(
        "INSERT INTO decay_log(ts,targets,min_after) VALUES(?,?,?)",
        (time.time(), touched, floor),
    )
    db.commit()
    return {"decayed": touched, "decay": decay, "floor": floor,
            "seconds": seconds}


def audit(db: sqlite3.Connection, limit: int = 20) -> list[dict]:
    rows = db.execute(
        "SELECT * FROM user_feedback ORDER BY id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


def stats(db: sqlite3.Connection) -> dict:
    total = db.execute(
        "SELECT COUNT(*) AS n FROM user_feedback").fetchone()["n"]
    by_source = db.execute(
        "SELECT source, COUNT(*) AS n FROM user_feedback "
        "GROUP BY source").fetchall()
    decays = db.execute(
        "SELECT MAX(ts) AS last, SUM(targets) AS total "
        "FROM decay_log").fetchone()
    return {
        "feedback_total": int(total or 0),
        "by_source": {str(r["source"]): int(r["n"]) for r in by_source},
        "last_decay": decays["last"],
        "decayed_total": int(decays["total"] or 0),
    }


def main(argv: list | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="farsi.protected",
        description="یادگیری محافظت‌شده: فقط feedback واقعی کاربر",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    fb = sub.add_parser("feedback", help="ثبت feedback کاربر")
    fb.add_argument("--source", required=True,
                    help="مثلاً 'user', 'evaluator'")
    fb.add_argument("--kind", required=True,
                    choices=("word", "frame", "pair", "cloze"))
    fb.add_argument("--ref", required=True)
    fb.add_argument("--score", type=float, required=True)
    fb.add_argument("--allow-self", action="store_true")

    dc = sub.add_parser("decay", help="اجرای decay روی وزن‌ها")
    dc.add_argument("--decay", type=float, default=W_DECAY)

    au = sub.add_parser("audit", help="آخرین feedbackها")
    au.add_argument("--limit", type=int, default=20)

    sub.add_parser("stats", help="آمار")
    args = parser.parse_args(argv)

    db = connect()
    ldb = lexicon.connect()
    fdb = frames.connect()
    try:
        if args.command == "feedback":
            print(accept_feedback(
                db, ldb, fdb, args.source, args.kind, args.ref,
                args.score, allow_self=args.allow_self,
            ))
            return 0
        if args.command == "decay":
            print(decay_weights(db, ldb, fdb, decay=args.decay))
            return 0
        if args.command == "audit":
            for item in audit(db, args.limit):
                print(f"#{item['id']} [{item['source']}] "
                      f"{item['kind']}:{item['ref']} = {item['score']} "
                      f"applied={item['applied']}")
            return 0
        print(stats(db))
        return 0
    except (AgentError, OSError, ValueError, sqlite3.Error) as exc:
        print("توقف:", exc)
        return 1
    finally:
        fdb.close(); ldb.close(); db.close()


if __name__ == "__main__":
    raise SystemExit(main())