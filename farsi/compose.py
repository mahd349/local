"""Compose new sentences from mined frames and learned words.

Nothing here stores a finished answer. A candidate is a frame key plus the
words chosen for its slots; the sentence is rendered on demand. The topic is
substituted into a slot whose original occupant is a distributional peer, so
the output is grounded in real Persian structure but is not a stored sentence.
"""
from __future__ import annotations

import argparse
import itertools
import json
import math
import sqlite3
import time
from pathlib import Path

from agent.config import STATE_DIR, assert_writable, ensure_dirs
from agent.errors import AgentError

from . import corpus, frames, lexicon, usage
from .text import tokens

COMPOSE_DB = STATE_DIR / "farsi_compose.db"

MAX_PEERS = 8
MAX_FRAMES_PER_PEER = 6
MAX_SLOT_OPTIONS = 4
MAX_RENDER = 60
FRAME_SATURATION = 50.0
MIN_FLUENCY_TERMS = 200

MIN_AUTO_WEIGHT = 1.3
MIN_AUTO_POSITIVE = 3
MIN_AUTO_MARGIN = 0.12

_SCHEMA = """
CREATE TABLE IF NOT EXISTS offers(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL, message TEXT, topic TEXT,
    status TEXT DEFAULT 'open', chosen INTEGER);
CREATE TABLE IF NOT EXISTS candidates(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    offer_id INTEGER NOT NULL, position INTEGER NOT NULL,
    fkey TEXT NOT NULL, slot_idx INTEGER, fillers TEXT NOT NULL,
    score REAL, rating REAL);
CREATE INDEX IF NOT EXISTS idx_candidates_offer ON candidates(offer_id);
CREATE TABLE IF NOT EXISTS assoc(
    topic TEXT, term TEXT,
    positive INTEGER DEFAULT 0, negative INTEGER DEFAULT 0,
    neutral INTEGER DEFAULT 0, updated_ts REAL,
    PRIMARY KEY(topic, term));
"""


def connect(path: str | Path | None = None) -> sqlite3.Connection:
    ensure_dirs()
    target = assert_writable(Path(path) if path else COMPOSE_DB)
    target.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(target))
    db.row_factory = sqlite3.Row
    db.executescript(_SCHEMA)
    return db


def assoc_weight(db: sqlite3.Connection, topic: str, term: str) -> float:
    row = db.execute(
        "SELECT positive, negative FROM assoc WHERE topic=? AND term=?",
        (topic, term),
    ).fetchone()
    if row is None:
        return 1.0
    positive = int(row["positive"] or 0)
    negative = int(row["negative"] or 0)
    return round(max(0.1, 2.0 * (positive + 1) / (positive + negative + 2)), 3)


def _bump_assoc(db: sqlite3.Connection, topic: str, term: str,
                value: float) -> None:
    positive = 1 if value >= 4.0 else 0
    negative = 1 if value <= 2.0 else 0
    neutral = 1 if not positive and not negative else 0
    now = time.time()
    row = db.execute("SELECT topic FROM assoc WHERE topic=? AND term=?",
                     (topic, term)).fetchone()
    if row is None:
        db.execute(
            "INSERT INTO assoc(topic,term,positive,negative,neutral,updated_ts) "
            "VALUES(?,?,?,?,?,?)",
            (topic, term, positive, negative, neutral, now),
        )
    else:
        db.execute(
            "UPDATE assoc SET positive=positive+?, negative=negative+?, "
            "neutral=neutral+?, updated_ts=? WHERE topic=? AND term=?",
            (positive, negative, neutral, now, topic, term),
        )


def _related_cached(ldb, udb, term: str, cache: dict) -> dict[str, float]:
    if term not in cache:
        cache[term] = {
            item["term"]: float(item["score"])
            for item in lexicon.related(ldb, udb, term, MAX_PEERS)
        }
    return cache[term]


def _slot_options(fdb, ldb, udb, fkey: str, idx: int, topic: str,
                  topic_peers: dict, cache: dict) -> list[tuple[str, float]]:
    observed = frames.slot_fillers(fdb, fkey, idx)[:3]
    options: dict[str, float] = {}

    for entry in observed:
        base = str(entry["term"])
        support = min(1.0, float(entry["n"]) / 5.0)
        for word, similarity in _related_cached(ldb, udb, base, cache).items():
            relevance = 1.0 if word == topic else topic_peers.get(word, 0.0)
            fit = 0.7 * similarity * support + 0.3 * relevance
            value = fit * lexicon.word_weight(ldb, word)
            if value > options.get(word, 0.0):
                options[word] = value

    ranked = sorted(options.items(), key=lambda item: (-item[1], item[0]))
    return ranked[:MAX_SLOT_OPTIONS]


def _fluency(udb, text: str, ready: bool) -> float | None:
    if not ready:
        return None
    try:
        value = float(usage.score(udb, text))
    except (sqlite3.Error, KeyError, ValueError):
        return None
    return round(max(0.0, min(1.0, 2.0 ** value)), 4)


def _known_sentences(cdb, keys_list: list[str]) -> set[str]:
    if not keys_list:
        return set()
    marks = ",".join("?" for _ in keys_list)
    rows = cdb.execute(
        f"SELECT keys FROM docs WHERE keys IN ({marks})", keys_list
    ).fetchall()
    return {str(row["keys"]) for row in rows}


def _frame_positive(fdb, fkey: str) -> int:
    row = fdb.execute("SELECT positive FROM frames WHERE fkey=?",
                      (fkey,)).fetchone()
    return int(row["positive"] or 0) if row else 0


def build(db, cdb, udb, ldb, fdb, text: str, limit: int = 2,
          exclude: set[str] | None = None) -> dict:
    info = lexicon.topic_of(ldb, udb, text)
    topic = info["topic"]
    blocked = set(exclude or ())

    report = {"topic": info["surface"], "topic_term": topic,
              "reason": info["reason"], "candidates": [],
              "seen": {"frames": 0, "rendered": 0, "already_known": 0,
                       "excluded": 0},
              "fluency_used": False}

    if not topic:
        return report

    cache: dict[str, dict[str, float]] = {}
    topic_peers = _related_cached(ldb, udb, topic, cache)
    fluency_ready = int(usage.stats(udb)["terms"]) >= MIN_FLUENCY_TERMS
    report["fluency_used"] = fluency_ready

    raw: list[dict] = []
    seen_frames: set[tuple[str, int]] = set()
    seen_keys: set[str] = set()

    for peer in [topic] + list(topic_peers):
        for frame in frames.frames_with_filler(fdb, peer, MAX_FRAMES_PER_PEER):
            fkey = str(frame["fkey"])
            idx = int(frame["idx"])
            if (fkey, idx) in seen_frames:
                continue
            seen_frames.add((fkey, idx))
            report["seen"]["frames"] += 1

            slot_fit = 1.0 if peer == topic else topic_peers.get(peer, 0.0)
            others = [i for i in range(int(frame["slots"])) if i != idx]
            pools = [_slot_options(fdb, ldb, udb, fkey, i, topic,
                                   topic_peers, cache) for i in others]
            if any(not pool for pool in pools):
                continue

            combos = itertools.product(*pools) if pools else [()]
            for number, combo in enumerate(combos):
                if number >= MAX_SLOT_OPTIONS ** 2:
                    break
                fillers = {idx: topic}
                other_fit = 1.0
                for slot, (word, value) in zip(others, combo):
                    fillers[slot] = word
                    other_fit = min(other_fit, value)

                try:
                    sentence = frames.render(fdb, ldb, fkey, fillers)
                except AgentError:
                    continue
                keys = " ".join(tokens(sentence))
                if not keys or keys in seen_keys:
                    continue
                seen_keys.add(keys)
                report["seen"]["rendered"] += 1

                raw.append({"fkey": fkey, "frame_ref": int(frame["rowid"]),
                            "slot_idx": idx, "fillers": fillers,
                            "text": sentence, "keys": keys,
                            "frame_n": float(frame["n"]),
                            "frame_weight": float(frame["weight"]),
                            "slot_fit": slot_fit, "other_fit": other_fit})
                if len(raw) >= MAX_RENDER:
                    break
            if len(raw) >= MAX_RENDER:
                break
        if len(raw) >= MAX_RENDER:
            break

    kept = [item for item in raw if item["fkey"] not in blocked]
    report["seen"]["excluded"] = len(raw) - len(kept)
    known = _known_sentences(cdb, [item["keys"] for item in kept])
    fresh = [item for item in kept if item["keys"] not in known]
    report["seen"]["already_known"] = len(kept) - len(fresh)

    if not fresh:
        if not raw:
            report["reason"] = "no-frames-for-topic"
        elif not kept:
            report["reason"] = "all-excluded"
        else:
            report["reason"] = "only-known-sentences"
        return report

    for item in fresh:
        support = min(1.0, math.log1p(item["frame_n"])
                      / math.log1p(FRAME_SATURATION))
        support *= item["frame_weight"] / 2.0
        terms = [word for word in item["fillers"].values() if word != topic]
        assoc = (sum(assoc_weight(db, topic, word) for word in terms)
                 / len(terms) / 2.0) if terms else 0.5
        fluency = _fluency(udb, item["text"], fluency_ready)
        item["fluency"] = fluency
        item["parts"] = {"frame": round(support, 4),
                         "slot_fit": round(item["slot_fit"], 4),
                         "other_fit": round(item["other_fit"], 4),
                         "assoc": round(assoc, 4),
                         "fluency": fluency if fluency is not None else 0.5}
        parts = item["parts"]
        item["score"] = round(0.35 * parts["frame"] + 0.30 * parts["slot_fit"]
                              + 0.15 * parts["other_fit"] + 0.10 * parts["assoc"]
                              + 0.10 * parts["fluency"], 4)

    fresh.sort(key=lambda item: (-item["score"], item["keys"]))
    return report | {"candidates": fresh[:max(1, int(limit))]}


def compose(db, cdb, udb, ldb, fdb, text: str, *, limit: int = 2,
            force_ask: bool = False, exclude: set[str] | None = None) -> dict:
    report = build(db, cdb, udb, ldb, fdb, text, limit, exclude=exclude)
    report["offer_id"] = None
    report["auto"] = False

    picked = report["candidates"]
    if not picked:
        return report

    cursor = db.execute(
        "INSERT INTO offers(ts,message,topic,status) VALUES(?,?,?,?)",
        (time.time(), str(text), report["topic_term"], "open"),
    )
    offer_id = int(cursor.lastrowid)

    for position, item in enumerate(picked, start=1):
        item["position"] = position
        db.execute(
            "INSERT INTO candidates(offer_id,position,fkey,slot_idx,fillers,score) "
            "VALUES(?,?,?,?,?,?)",
            (offer_id, position, item["fkey"], item["slot_idx"],
             json.dumps({str(k): v for k, v in item["fillers"].items()},
                        ensure_ascii=False), item["score"]),
        )
    db.commit()
    report["offer_id"] = offer_id

    if force_ask or len(picked) < 2:
        return report

    best, second = picked[0], picked[1]
    if (best["frame_weight"] >= MIN_AUTO_WEIGHT
            and _frame_positive(fdb, best["fkey"]) >= MIN_AUTO_POSITIVE
            and best["score"] - second["score"] >= MIN_AUTO_MARGIN):
        db.execute("UPDATE offers SET status='auto', chosen=1 WHERE id=?",
                   (offer_id,))
        db.commit()
        report["auto"] = True

    return report


def _offer_candidates(db, offer_id: int) -> list[dict]:
    rows = db.execute(
        "SELECT * FROM candidates WHERE offer_id=? ORDER BY position",
        (int(offer_id),),
    ).fetchall()
    out = []
    for row in rows:
        item = dict(row)
        item["fillers"] = {int(k): v for k, v in
                           json.loads(item["fillers"]).items()}
        out.append(item)
    return out


def rate(db, fdb, ldb, offer_id: int, *, choice: int | None = None,
         scores: dict | None = None) -> dict:
    offer = db.execute("SELECT * FROM offers WHERE id=?",
                       (int(offer_id),)).fetchone()
    if offer is None:
        raise AgentError(f"پیشنهاد {offer_id} پیدا نشد")
    items = _offer_candidates(db, offer_id)
    if not items:
        raise AgentError("این پیشنهاد نامزدی ندارد")
    if choice is None and not scores:
        raise AgentError("choice یا scores لازم است")

    values: dict[int, float] = {}
    if choice is not None:
        if int(choice) not in {item["position"] for item in items}:
            raise AgentError("این نامزد وجود ندارد")
        for item in items:
            values[item["position"]] = 5.0 if item["position"] == int(choice) else 1.0
    else:
        for raw_position, raw_value in (scores or {}).items():
            position = int(raw_position)
            value = float(raw_value)
            if position not in {item["position"] for item in items}:
                raise AgentError("این نامزد وجود ندارد")
            if not 0.0 <= value <= 5.0:
                raise AgentError("نمره باید بین 0 تا 5 باشد")
            values[position] = value
    if not values:
        raise AgentError("هیچ نمره‌ای داده نشد")

    topic = str(offer["topic"] or "")
    updates = []

    for item in items:
        value = values.get(item["position"])
        if value is None:
            continue
        frames.rate_frame(fdb, item["fkey"], value)
        for word in item["fillers"].values():
            for part in tokens(word):
                lexicon.rate_word(ldb, part, value)
                if topic and part != topic:
                    _bump_assoc(db, topic, part, value)
        db.execute("UPDATE candidates SET rating=? WHERE id=?",
                   (value, item["id"]))
        updates.append({"position": item["position"], "score": value,
                        "fkey": item["fkey"]})

    best = max(values.items(), key=lambda pair: pair[1])[0]
    db.execute("UPDATE offers SET status='rated', chosen=? WHERE id=?",
               (best, int(offer_id)))
    db.commit()
    return {"offer_id": int(offer_id), "updates": updates}


def list_offers(db, fdb, ldb, limit: int = 10) -> list[dict]:
    rows = db.execute("SELECT * FROM offers ORDER BY id DESC LIMIT ?",
                      (max(1, int(limit)),)).fetchall()
    out = []
    for row in rows:
        item = dict(row)
        item["candidates"] = []
        for candidate in _offer_candidates(db, item["id"]):
            try:
                candidate["text"] = frames.render(fdb, ldb, candidate["fkey"],
                                                  candidate["fillers"])
            except AgentError:
                candidate["text"] = ""
            item["candidates"].append(candidate)
        out.append(item)
    return out


def stats(db) -> dict:
    total = db.execute("SELECT COUNT(*) AS n FROM offers").fetchone()["n"]
    rated = db.execute(
        "SELECT COUNT(*) AS n FROM offers WHERE status='rated'").fetchone()["n"]
    auto = db.execute(
        "SELECT COUNT(*) AS n FROM offers WHERE status='auto'").fetchone()["n"]
    pairs = db.execute("SELECT COUNT(*) AS n FROM assoc").fetchone()["n"]
    return {"offers": int(total or 0), "rated": int(rated or 0),
            "auto": int(auto or 0), "assoc_pairs": int(pairs or 0)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="farsi.compose", description="ساختن جملهٔ نو از اسکلت و واژه")
    sub = parser.add_subparsers(dest="command", required=True)

    say = sub.add_parser("say", help="دربارهٔ متن، جملهٔ نو بساز")
    say.add_argument("text")
    say.add_argument("--limit", type=int, default=2)
    say.add_argument("--force-ask", action="store_true")

    rt = sub.add_parser("rate", help="امتیاز دادن به نامزدها")
    rt.add_argument("offer_id", type=int)
    rt.add_argument("--choice", type=int, default=None)
    rt.add_argument("--score", action="append", default=[],
                    help="مثال: --score 1=5 --score 2=2")

    of = sub.add_parser("offers", help="پیشنهادها، بازسازی‌شده از قطعات")
    of.add_argument("--limit", type=int, default=10)

    sub.add_parser("stats", help="آمار")

    args = parser.parse_args(argv)

    db = connect()
    fdb = frames.connect()
    ldb = lexicon.connect()
    udb = usage.connect()
    cdb = corpus.connect()

    try:
        if args.command == "say":
            report = compose(db, cdb, udb, ldb, fdb, args.text,
                             limit=args.limit, force_ask=args.force_ask)
            print("موضوع:", report["topic"] or "-")
            if not report["candidates"]:
                print("دلیل:", report["reason"] or "-")
                print("آمار:", report["seen"])
                return 0
            print("پیشنهاد:", report["offer_id"],
                  "| خودکار:", "بله" if report["auto"] else "خیر",
                  "| روانی:", report["fluency_used"])
            for item in report["candidates"]:
                print(f"\n{item['position']}. {item['text']}")
                print("   ", item["parts"], "| اسکلت:", item["frame_ref"])
            print("\nامتیاز:")
            print(f"python -m farsi.compose rate {report['offer_id']} --choice 1")
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
            print(rate(db, fdb, ldb, args.offer_id,
                       choice=args.choice, scores=scores or None))
            return 0

        if args.command == "offers":
            for item in list_offers(db, fdb, ldb, args.limit):
                print(f"#{item['id']} [{item['status']}] موضوع: {item['topic']}")
                for candidate in item["candidates"]:
                    print(f"   {candidate['position']}. {candidate['text']}")
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
        fdb.close()
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())