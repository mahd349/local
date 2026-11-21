"""Next-word prediction layer on top of the usage engine.
Generates a sentence word by word: start from the topic, ask usage.next_options
for continuations, re-rank them with learned word weights and local pair
feedback, and advance a narrow beam. Ending is learned, not written: a word may
close a sentence only as often as real corpus sentences closed with it, so
Persian function words cannot leave a dangling tail. Candidates must differ
substantially, and shared parts of two ratings receive the mean instead of
contradictory signals. Nothing stores a finished sentence: an offer keeps only
token keys and is re-rendered on demand. A hard query budget bounds every run.
"""
from __future__ import annotations

import argparse
import json
import math
import random
import sqlite3
import time
from functools import lru_cache
from pathlib import Path

from agent.config import STATE_DIR, assert_writable, ensure_dirs
from agent.errors import AgentError

from . import corpus, lexicon, usage
from .text import sentences, tokens

PREDICT_DB = STATE_DIR / "farsi_predict.db"
BEAM = 4
OPTIONS = 6
MAX_STEPS = 16
MIN_STEPS = 5
MAX_QUERIES = 400
COLLAPSE_P = 0.02
FINAL_MIN = 0.25
OVERLAP_MAX = 0.6

_SCHEMA = """
CREATE TABLE IF NOT EXISTS offers(
id INTEGER PRIMARY KEY AUTOINCREMENT,
ts REAL, message TEXT, topic TEXT,
status TEXT DEFAULT 'open', chosen INTEGER);
CREATE TABLE IF NOT EXISTS candidates(
id INTEGER PRIMARY KEY AUTOINCREMENT,
offer_id INTEGER NOT NULL, position INTEGER NOT NULL,
words TEXT NOT NULL, score REAL, rating REAL);
CREATE INDEX IF NOT EXISTS idx_pcand_offer ON candidates(offer_id);
CREATE TABLE IF NOT EXISTS pair_stats(
a TEXT, b TEXT,
positive INTEGER DEFAULT 0, negative INTEGER DEFAULT 0,
neutral INTEGER DEFAULT 0, updated_ts REAL,
PRIMARY KEY(a, b));
"""


def connect(path: str | Path | None = None) -> sqlite3.Connection:
    ensure_dirs()
    target = assert_writable(Path(path) if path else PREDICT_DB)
    target.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(target))
    db.row_factory = sqlite3.Row
    db.executescript(_SCHEMA)
    return db


def pair_weight(db: sqlite3.Connection, a: str, b: str) -> float:
    row = db.execute(
        "SELECT positive, negative FROM pair_stats WHERE a=? AND b=?",
        (a, b)).fetchone()
    if row is None:
        return 1.0
    positive = int(row["positive"] or 0)
    negative = int(row["negative"] or 0)
    return round(max(0.1, 2.0 * (positive + 1) / (positive + negative + 2)), 3)


def _bump_pair(db: sqlite3.Connection, a: str, b: str, value: float) -> None:
    positive = 1 if value >= 4.0 else 0
    negative = 1 if value <= 2.0 else 0
    neutral = 1 if not positive and not negative else 0
    db.execute(
        "INSERT INTO pair_stats(a,b,positive,negative,neutral,updated_ts) "
        "VALUES(?,?,?,?,?,?) "
        "ON CONFLICT(a,b) DO UPDATE SET positive=positive+excluded.positive, "
        "negative=negative+excluded.negative, neutral=neutral+excluded.neutral, "
        "updated_ts=excluded.updated_ts",
        (a, b, positive, negative, neutral, time.time()),
    )


@lru_cache(maxsize=1)
def _sentence_lengths() -> tuple[int, ...]:
    """Target lengths come from real corpus sentences, sampled once."""
    cdb = corpus.connect()
    try:
        rows = cdb.execute(
            "SELECT raw FROM docs ORDER BY id LIMIT 200").fetchall()
    finally:
        cdb.close()
    lengths: list[int] = []
    for row in rows:
        for sentence in sentences(row["raw"] or ""):
            n = len(tokens(sentence))
            if 4 <= n <= 24:
                lengths.append(n)
    return tuple(lengths[:300]) or (10,)


@lru_cache(maxsize=1)
def _final_stats() -> dict[str, tuple[int, int]]:
    """How often a word actually closes a sentence in the corpus.

    Persian glue words are almost never sentence-final; verbs and copulas are.
    This is measured from text, never from a hand-written word list.
    """
    cdb = corpus.connect()
    try:
        rows = cdb.execute(
            "SELECT raw FROM docs ORDER BY id LIMIT 200").fetchall()
    finally:
        cdb.close()
    totals: dict[str, int] = {}
    finals: dict[str, int] = {}
    for row in rows:
        for sentence in sentences(row["raw"] or ""):
            keys = tokens(sentence)
            if not keys:
                continue
            for key in set(keys):
                totals[key] = totals.get(key, 0) + 1
            finals[keys[-1]] = finals.get(keys[-1], 0) + 1
    return {key: (finals.get(key, 0), count)
            for key, count in totals.items()}


def final_score(term: str) -> float:
    """Smoothed share of the sentences containing this word that end with it."""
    final, total = _final_stats().get(term, (0, 0))
     return (final + 0.3) / (total + 2.0)


def _complete(keys: tuple[str, ...]) -> tuple[str, ...]:
    """Drop a dangling tail; return empty if no honest ending remains."""
    trimmed = keys
    while len(trimmed) > MIN_STEPS and final_score(trimmed[-1]) < FINAL_MIN:
        trimmed = trimmed[:-1]
    if len(trimmed) < MIN_STEPS or final_score(trimmed[-1]) < FINAL_MIN:
        return ()
    return trimmed


def _next_options(udb: sqlite3.Connection, phrase: list[str],
                  limit: int) -> list[tuple[str, float]]:
    """usage.next_options takes a string prefix; tolerate a list too."""
    raw = None
    for argument in (" ".join(phrase), phrase):
        try:
            raw = usage.next_options(udb, argument, limit=limit)
            break
        except TypeError:
            try:
                raw = usage.next_options(udb, argument)
                break
            except TypeError:
                continue
    if raw is None:
        return []
    out: list[tuple[str, float]] = []
    for item in raw:
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
    return out[:limit]


def _probabilities(items: list[tuple[str, float]]) -> dict[str, float]:
    if not items:
        return {}
    values = [score for _, score in items]
    if max(values) <= 0.0:
        weights = [math.exp(max(-20.0, min(20.0, s))) for s in values]
    else:
        weights = [max(0.0, s) for s in values]
    total = sum(weights) or 1.0
    return {term: w / total for (term, _), w in zip(items, weights)}


def generate(db, cdb, udb, ldb, text: str, *, limit: int = 2,
             beam: int = BEAM, max_steps: int = MAX_STEPS,
             budget: int = MAX_QUERIES) -> dict:
    info = lexicon.topic_of(ldb, udb, text)
    topic = info["topic"]
    report = {"topic": info["surface"], "topic_term": topic,
              "reason": info["reason"], "candidates": [],
              "queries": 0, "fluency_used": False}
    if not topic:
        return report
    rng = random.Random()
    lengths = _sentence_lengths()
    beams = [((topic,), 0.0, rng.choice(lengths))]
    finished: list[tuple[tuple[str, ...], float]] = []
    queries = 0
    for _ in range(max_steps):
        if not beams:
            break
        grown: list[tuple[tuple[str, ...], float, int]] = []
        for keys, logp, target in beams:
            ready = (len(keys) >= MIN_STEPS
                     and final_score(keys[-1]) >= FINAL_MIN)
            if len(keys) >= target and ready:
                finished.append(
                    (keys, logp + math.log(final_score(keys[-1]))))
                continue
            queries += 1
            if queries > budget:
                finished.append((_complete(keys), logp))
                continue
            options = _next_options(udb, list(keys[-2:]), OPTIONS)
            probs = _probabilities(options)
            if not probs:
                finished.append((_complete(keys), logp))
                continue
            if ready and max(probs.values()) < COLLAPSE_P:
                finished.append((keys, logp))
                continue
            for term, p in probs.items():
                if term == keys[-1]:
                    continue
                adjusted = (p * lexicon.word_weight(ldb, term)
                            * pair_weight(db, keys[-1], term))
                grown.append((keys + (term,),
                              logp + math.log(adjusted + 1e-9), target))
        grown.sort(key=lambda item: -item[1])
        beams = grown[:beam]
    for keys, logp, _ in beams:
        finished.append((_complete(keys), logp))
    finished = [(keys, logp) for keys, logp in finished if keys]
    report["queries"] = queries
    if not finished:
        report["reason"] = "no-complete-ending"
        return report
    raw: list[dict] = []
    for keys, logp in sorted(finished, key=lambda item: -item[1]):
        mine = set(keys)
        too_close = False
        for item in raw:
            other = set(item["keys"])
            union = mine | other
            if union and len(mine & other) / len(union) > OVERLAP_MAX:
                too_close = True
                break
        if too_close:
            continue
        raw.append({"keys": keys, "logp": logp})
    marks = ",".join("?" for _ in raw)
    known = {str(r["keys"]) for r in cdb.execute(
        f"SELECT keys FROM docs WHERE keys IN ({marks})",
        [" ".join(item["keys"]) for item in raw]).fetchall()}
    fresh = [item for item in raw if " ".join(item["keys"]) not in known]
    if not fresh:
        report["reason"] = "only-known-sentences"
        return report
    fluency_ready = int(usage.stats(udb)["terms"]) >= 200
    report["fluency_used"] = fluency_ready
    for item in fresh:
        sentence = " ".join(lexicon.surface_for(ldb, k) for k in item["keys"])
        fluency = 0.5
        if fluency_ready:
            try:
                fluency = max(0.0, min(1.0, 2.0 ** float(
                    usage.score(udb, sentence))))
            except (sqlite3.Error, KeyError, ValueError):
                fluency = 0.5
        item["text"] = sentence
        item["fluency"] = round(fluency, 4)
        item["score"] = round(0.7 * fluency + 0.3 * min(
            1.0, math.exp(item["logp"] / max(1, len(item["keys"])))), 4)
    fresh.sort(key=lambda item: (-item["score"], item["text"]))
    report["candidates"] = fresh[:max(1, int(limit))]
    return report


def say(db, cdb, udb, ldb, text: str, **kwargs) -> dict:
    report = generate(db, cdb, udb, ldb, text, **kwargs)
    report["offer_id"] = None
    picked = report["candidates"]
    if not picked:
        return report
    cursor = db.execute(
        "INSERT INTO offers(ts,message,topic,status) VALUES(?,?,?,?)",
        (time.time(), str(text), report["topic_term"], "open"))
    offer_id = int(cursor.lastrowid)
    for position, item in enumerate(picked, start=1):
        item["position"] = position
        db.execute(
            "INSERT INTO candidates(offer_id,position,words,score) "
            "VALUES(?,?,?,?)",
            (offer_id, position,
             json.dumps(list(item["keys"]), ensure_ascii=False),
             item["score"]))
    db.commit()
    report["offer_id"] = offer_id
    return report


def rate(db, ldb, offer_id: int, *, choice: int | None = None,
         scores: dict | None = None) -> dict:
    offer = db.execute("SELECT * FROM offers WHERE id=?",
                       (int(offer_id),)).fetchone()
    if offer is None:
        raise AgentError(f"پیشنهاد {offer_id} پیدا نشد")
    rows = db.execute(
        "SELECT * FROM candidates WHERE offer_id=? ORDER BY position",
        (int(offer_id),)).fetchall()
    if not rows:
        raise AgentError("این پیشنهاد نامزدی ندارد")
    if choice is None and not scores:
        raise AgentError("choice یا scores لازم است")
    values: dict[int, float] = {}
    if choice is not None:
        if int(choice) not in {r["position"] for r in rows}:
            raise AgentError("این نامزد وجود ندارد")
        for row in rows:
            values[row["position"]] = (
                5.0 if row["position"] == int(choice) else 1.0)
    else:
        for raw_position, raw_value in (scores or {}).items():
            position, value = int(raw_position), float(raw_value)
            if position not in {r["position"] for r in rows}:
                raise AgentError("این نامزد وجود ندارد")
            if not 0.0 <= value <= 5.0:
                raise AgentError("نمره باید بین 0 تا 5 باشد")
            values[position] = value
    word_scores: dict[str, list[float]] = {}
    pair_scores: dict[tuple[str, str], list[float]] = {}
    updates = []
    for row in rows:
        value = values.get(row["position"])
        if value is None:
            continue
        words = json.loads(row["words"])
        for word in words:
            word_scores.setdefault(word, []).append(value)
        for a, b in zip(words, words[1:]):
            pair_scores.setdefault((a, b), []).append(value)
        db.execute("UPDATE candidates SET rating=? WHERE id=?",
                   (value, row["id"]))
        updates.append({"position": row["position"], "score": value})
    # A word shared by two candidates must not get both a positive and a
    # negative signal; it receives the mean of the scores instead.
    for word, marks in word_scores.items():
        lexicon.rate_word(ldb, word, sum(marks) / len(marks))
    for (a, b), marks in pair_scores.items():
        _bump_pair(db, a, b, sum(marks) / len(marks))
    best = max(values.items(), key=lambda pair: pair[1])[0]
    db.execute("UPDATE offers SET status='rated', chosen=? WHERE id=?",
               (best, int(offer_id)))
    db.commit()
    return {"offer_id": int(offer_id), "updates": updates}


def stats(db) -> dict:
    offers = db.execute("SELECT COUNT(*) AS n FROM offers").fetchone()["n"]
    rated = db.execute(
        "SELECT COUNT(*) AS n FROM offers WHERE status='rated'").fetchone()["n"]
    pairs = db.execute("SELECT COUNT(*) AS n FROM pair_stats").fetchone()["n"]
    return {"offers": int(offers or 0), "rated": int(rated or 0),
            "pairs": int(pairs or 0)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="farsi.predict", description="تولید کلمه‌به‌کلمه با پیش‌بینی")
    sub = parser.add_subparsers(dest="command", required=True)
    say_p = sub.add_parser("say", help="جمله را کلمه‌به‌کلمه بساز")
    say_p.add_argument("text")
    say_p.add_argument("--limit", type=int, default=2)
    say_p.add_argument("--beam", type=int, default=BEAM)
    say_p.add_argument("--steps", type=int, default=MAX_STEPS)
    say_p.add_argument("--budget", type=int, default=MAX_QUERIES)
    rate_p = sub.add_parser("rate", help="امتیاز به نامزدها")
    rate_p.add_argument("offer_id", type=int)
    rate_p.add_argument("--choice", type=int, default=None)
    rate_p.add_argument("--score", action="append", default=[])
    end_p = sub.add_parser("ending", help="آمار پایان‌بندی یک واژه")
    end_p.add_argument("word")
    sub.add_parser("stats", help="آمار")
    args = parser.parse_args(argv)
    db = connect()
    ldb = lexicon.connect()
    udb = usage.connect()
    cdb = corpus.connect()
    try:
        if args.command == "say":
            report = say(db, cdb, udb, ldb, args.text, limit=args.limit,
                         beam=args.beam, max_steps=args.steps,
                         budget=args.budget)
            print("موضوع:", report["topic"] or "-",
                  "| پرس‌وجو:", report["queries"])
            if not report["candidates"]:
                print("دلیل:", report["reason"] or "-")
                return 0
            print("پیشنهاد:", report["offer_id"])
            for item in report["candidates"]:
                print(f"\n{item['position']}. {item['text']}")
                print("   ", {"fluency": item["fluency"],
                              "score": item["score"],
                              "پایان": round(final_score(item["keys"][-1]), 3)})
            print(f"\npython -m farsi.predict rate {report['offer_id']} --choice 1")
            return 0
        if args.command == "rate":
            scores: dict[int, float] = {}
            for item in args.score:
                if "=" not in item:
                    raise AgentError("فرمت score باید مثل 1=5 باشد")
                position, value = item.split("=", 1)
                scores[int(position)] = float(value)
            if args.choice is not None and scores:
                raise AgentError("فقط یکی از --choice یا --score")
            print(rate(db, ldb, args.offer_id, choice=args.choice,
                       scores=scores or None))
            return 0
        if args.command == "ending":
            key = (tokens(args.word) or [""])[0]
            final, total = _final_stats().get(key, (0, 0))
            print("واژه:", key, "| پایان‌بندی:", final, "از", total,
                  "| نمره:", round(final_score(key), 3),
                  "| آستانه:", FINAL_MIN)
            return 0
        print(stats(db))
        return 0
    except (AgentError, OSError, ValueError, sqlite3.Error) as exc:
        print("توقف:", exc)
        return 1
    finally:
        cdb.close()
        udb.close()
        ldb.close()
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())