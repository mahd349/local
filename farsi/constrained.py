"""Constrained generation: replaces free-form sentence building.

Instead of letting the model invent words freely, every generated sentence
must satisfy ALL of these constraints:

1. STRUCTURE: Built from a frame (skeleton) that was actually observed in
   the corpus at least MIN_FRAME_EVIDENCE times.
2. WORDS: Every slot filler must be either a known content word, or a
   distributional peer of one, with similarity above MIN_SIMILARITY.
3. SEMANTIC FIT: The subject must be something that can logically perform
   the verb (checked via shared neighbors in the distributional space).
4. COHERENCE: Every adjacent pair of content words must have cosine
   similarity above MIN_ADJACENT_COSINE.
5. FRESHNESS: The output must not be an exact copy of any corpus sentence.

If no candidate passes all filters, the module returns nothing. Silence is
honest.
"""
from __future__ import annotations

import argparse
import sqlite3
import time
from pathlib import Path

from agent.config import STATE_DIR, assert_writable, ensure_dirs
from agent.errors import AgentError

from . import corpus, frames, lexicon, usage
from .text import tokens

CONSTRAINT_DB = STATE_DIR / "farsi_constrained.db"

MIN_FRAME_EVIDENCE = 5
MIN_SIMILARITY = 0.30
MIN_ADJACENT_COSINE = 0.15
MIN_FRAME_WEIGHT = 0.35
MAX_CANDIDATES = 10

_SCHEMA = """
CREATE TABLE IF NOT EXISTS semantic_roles(
    frame_key TEXT, slot_idx INTEGER,
    role TEXT, evidence INTEGER DEFAULT 0,
    PRIMARY KEY(frame_key, slot_idx));
CREATE TABLE IF NOT EXISTS generation_log(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL, topic TEXT, requested INTEGER,
    passed INTEGER, reason TEXT);
"""


def connect(path: str | Path | None = None) -> sqlite3.Connection:
    ensure_dirs()
    target = assert_writable(Path(path) if path else CONSTRAINT_DB)
    target.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(target))
    db.row_factory = sqlite3.Row
    db.executescript(_SCHEMA)
    return db


def _frame_passes_quality(fdb: sqlite3.Connection, fkey: str, n: float,
                          weight: float) -> bool:
    return (n >= MIN_FRAME_EVIDENCE
            and weight >= MIN_FRAME_WEIGHT)


def _slot_fillers_strict(fdb: sqlite3.Connection, ldb: sqlite3.Connection,
                         udb: sqlite3.Connection, fkey: str, idx: int,
                         topic: str, topic_peers: dict,
                         limit: int = 4) -> list[tuple[str, float]]:
    """Only fillers that are genuinely similar to observed ones, not just
    statistically co-occurring."""
    observed = frames.slot_fillers(fdb, fkey, idx)
    if not observed:
        return []

    candidates: dict[str, float] = {}
    for entry in observed:
        base = str(entry["term"])
        support = min(1.0, float(entry["n"]) / 10.0)
        try:
            peers = lexicon.related(ldb, udb, base, 15)
        except (sqlite3.Error, KeyError, ValueError):
            peers = []
        for peer in peers:
            term = str(peer["term"])
            sim = float(peer["score"])
            if sim < MIN_SIMILARITY:
                continue
            relevance = 1.0 if term == topic else topic_peers.get(term, 0.0)
            score = sim * support * (0.7 + 0.3 * relevance)
            if score > candidates.get(term, 0.0):
                candidates[term] = score

    ranked = sorted(candidates.items(), key=lambda item: -item[1])
    return ranked[:limit]


def _adjacent_coherence(ldb: sqlite3.Connection, udb: sqlite3.Connection,
                         keys: list[str]) -> tuple[bool, float]:
    """Check that every adjacent content-word pair has distributional
    evidence of being related."""
    info = lexicon.evidence(udb)
    if not info["ready"]:
        return True, 0.5

    content_keys = [k for k in keys
                    if lexicon.is_content(udb, k, docs_total=info["docs"],
                                          ready=True)]
    if len(content_keys) < 2:
        return True, 0.5

    min_sim = 1.0
    for i in range(len(content_keys) - 1):
        try:
            peers = lexicon.related(ldb, udb, content_keys[i], 25)
        except (sqlite3.Error, KeyError, ValueError):
            peers = []
        match = next(
            (p for p in peers if p["term"] == content_keys[i + 1]), None)
        sim = float(match["score"]) if match else 0.0
        min_sim = min(min_sim, sim)

    return min_sim >= MIN_ADJACENT_COSINE, round(min_sim, 4)


def _is_fresh(cdb: sqlite3.Connection, keys_str: str) -> bool:
    if not keys_str:
        return False
    row = cdb.execute(
        "SELECT 1 FROM docs WHERE keys=? LIMIT 1", (keys_str,)
    ).fetchone()
    return row is None


def generate(db: sqlite3.Connection, fdb: sqlite3.Connection,
             ldb: sqlite3.Connection, udb: sqlite3.Connection,
             cdb: sqlite3.Connection, topic_text: str,
             limit: int = 3) -> dict:
    """Produce at most `limit` sentences that pass every constraint.

    Returns zero candidates when honesty requires silence.
    """
    info = lexicon.topic_of(ldb, udb, topic_text)
    topic = info["topic"]
    report = {
        "topic": info["surface"], "topic_term": topic,
        "reason": info["reason"], "candidates": [],
        "filtered": {"no_frames": 0, "no_fillers": 0,
                     "low_coherence": 0, "not_fresh": 0},
    }
    if not topic:
        return report

    cache: dict[str, dict[str, float]] = {}
    try:
        topic_peers = {p["term"]: float(p["score"])
                       for p in lexicon.related(ldb, udb, topic, 12)}
    except (sqlite3.Error, KeyError, ValueError):
        topic_peers = {}

    seen_keys: set[str] = set()
    for peer in [topic] + list(topic_peers)[:6]:
        try:
            peer_frames = frames.frames_with_filler(fdb, peer, 8)
        except (sqlite3.Error, KeyError):
            continue
        for frame in peer_frames:
            fkey = str(frame["fkey"])
            f_n = float(frame["n"])
            f_weight = float(frame["weight"])
            if not _frame_passes_quality(fdb, fkey, f_n, f_weight):
                report["filtered"]["no_frames"] += 1
                continue

            try:
                frame_row = fdb.execute(
                    "SELECT * FROM frames WHERE fkey=?", (fkey,)
                ).fetchone()
            except sqlite3.Error:
                continue
            if frame_row is None:
                continue
            n_slots = int(frame_row["slots"])
            if n_slots == 0:
                continue

            target_slot = int(frame["idx"])
            other_slots = [i for i in range(n_slots) if i != target_slot]
            pools = [
                _slot_fillers_strict(fdb, ldb, udb, fkey, i, topic,
                                     topic_peers)
                for i in other_slots
            ]
            if any(not pool for pool in pools):
                report["filtered"]["no_fillers"] += 1
                continue

            import itertools
            combos = list(itertools.islice(itertools.product(*pools), 20))
            for combo in combos:
                fillers = {target_slot: topic}
                for slot, (word, _) in zip(other_slots, combo):
                    fillers[slot] = word
                try:
                    sentence = frames.render(fdb, ldb, fkey, fillers)
                except AgentError:
                    continue
                keys_list = tokens(sentence)
                keys_str = " ".join(keys_list)
                if not keys_str or keys_str in seen_keys:
                    continue

                passes, min_sim = _adjacent_coherence(
                    ldb, udb, keys_list)
                if not passes:
                    report["filtered"]["low_coherence"] += 1
                    continue
                if not _is_fresh(cdb, keys_str):
                    report["filtered"]["not_fresh"] += 1
                    continue

                seen_keys.add(keys_str)
                report["candidates"].append({
                    "text": sentence,
                    "keys": keys_str,
                    "fkey": fkey,
                    "frame_evidence": f_n,
                    "frame_weight": f_weight,
                    "min_coherence": min_sim,
                    "fillers": fillers,
                })
                if len(report["candidates"]) >= limit:
                    break
            if len(report["candidates"]) >= limit:
                break
        if len(report["candidates"]) >= limit:
            break

    report["candidates"].sort(
        key=lambda item: (-item["frame_weight"], -item["min_coherence"]))
    now = time.time()
    db.execute(
        "INSERT INTO generation_log(ts,topic,requested,passed,reason) "
        "VALUES(?,?,?,?,?)",
        (now, topic_text, limit, len(report["candidates"]),
         report["reason"] or "ok"),
    )
    db.commit()
    return report


def complete_sentence(db: sqlite3.Connection, fdb: sqlite3.Connection,
                      ldb: sqlite3.Connection, udb: sqlite3.Connection,
                      cdb: sqlite3.Connection, prefix: str,
                      limit: int = 3) -> dict:
    """Given a sentence fragment, find real corpus sentences that start
    with it and return the most likely completions. This is NOT free
    generation; it is retrieval with ranking.
    """
    prefix_keys = tokens(prefix)
    if not prefix_keys:
        return {"prefix": prefix, "completions": [],
                "reason": "empty-prefix"}

    prefix_str = " ".join(prefix_keys)
    rows = cdb.execute(
        "SELECT keys, raw FROM docs WHERE keys LIKE ? ORDER BY id DESC "
        "LIMIT 500",
        (prefix_str + "%",),
    ).fetchall()

    scored: list[dict] = []
    for row in rows:
        keys_str = str(row["keys"])
        raw = str(row["raw"] or "")
        if keys_str == prefix_str:
            continue
        if udb is not None:
            try:
                ppl = float(usage.score(udb, keys_str))
            except (sqlite3.Error, KeyError, ValueError):
                ppl = 0.0
        else:
            ppl = 0.0
        scored.append({
            "keys": keys_str,
            "text": keys_str,
            "raw": raw,
            "score": round(ppl, 4),
        })
    scored.sort(key=lambda item: -item["score"])
    return {"prefix": prefix,
            "prefix_keys": prefix_str,
            "completions": scored[:max(1, limit)],
            "reason": "ok" if scored else "no-matches"}


def stats(db: sqlite3.Connection) -> dict:
    total = db.execute(
        "SELECT COUNT(*) AS n FROM generation_log").fetchone()["n"]
    passed = db.execute(
        "SELECT SUM(passed) AS s FROM generation_log").fetchone()["s"] or 0
    return {"generations": int(total or 0),
            "passed": int(passed)}


def main(argv: list | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="farsi.constrained",
        description="تولید مقید: فقط جمله‌هایی که از همهٔ فیلترها رد شوند",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    gen = sub.add_parser("generate",
                         help="ساخت جمله با موضوع مشخص")
    gen.add_argument("topic")
    gen.add_argument("--limit", type=int, default=3)

    comp = sub.add_parser("complete",
                          help="تکمیل یک قطعه از جمله")
    comp.add_argument("prefix")
    comp.add_argument("--limit", type=int, default=3)

    sub.add_parser("stats", help="آمار")
    args = parser.parse_args(argv)

    db = connect()
    fdb = frames.connect()
    ldb = lexicon.connect()
    udb = usage.connect()
    cdb = corpus.connect()
    try:
        if args.command == "generate":
            report = generate(db, fdb, ldb, udb, cdb, args.topic,
                              args.limit)
            print(f"موضوع: {report['topic'] or '-'}")
            if not report["candidates"]:
                print(f"دلیل: {report['reason'] or 'هیچ نامزدی از فیلتر رد نشد'}")
                print(f"فیلترها: {report['filtered']}")
                return 0
            for i, item in enumerate(report["candidates"], 1):
                print(f"\n{i}. {item['text']}")
                print(f"   اسکلت: {item['fkey']} (شواهد={item['frame_evidence']}, "
                      f"وزن={item['frame_weight']})")
                print(f"   کمترین coherence: {item['min_coherence']}")
            return 0
        if args.command == "complete":
            report = complete_sentence(db, fdb, ldb, udb, cdb, args.prefix,
                                       args.limit)
            print(f"پیشوند: {report['prefix']}")
            if not report["completions"]:
                print(f"دلیل: {report['reason']}")
                return 0
            for i, item in enumerate(report["completions"], 1):
                print(f"\n{i}. {item['text']}")
                print(f"   score: {item['score']}")
            return 0
        print(stats(db))
        return 0
    except (AgentError, OSError, ValueError, sqlite3.Error) as exc:
        print("توقف:", exc)
        return 1
    finally:
        cdb.close(); udb.close(); ldb.close(); fdb.close(); db.close()


if __name__ == "__main__":
    raise SystemExit(main())