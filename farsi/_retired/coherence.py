"""Advanced coherence scoring. FIXED: reads frame_slots + realistic thresholds."""
from __future__ import annotations

import argparse
import json
import math
import sqlite3
import time
from pathlib import Path

from agent.config import STATE_DIR, assert_writable, ensure_dirs
from agent.errors import AgentError

from . import frames, usage
from .text import tokens

COHERENCE_DB = STATE_DIR / "farsi_coherence.db"

MIN_VECTOR_SIZE = 20
MIN_TOPICAL_COSINE = 0.15
MIN_PAIR_PPMI = 0.03
ROLE_EVIDENCE_MIN = 4
ROLE_CLUSTER_THRESHOLD = 0.25

_SCHEMA = """
CREATE TABLE IF NOT EXISTS role_clusters(
    frame_key TEXT, slot_idx INTEGER,
    cluster_id INTEGER, label TEXT, evidence INTEGER,
    updated_ts REAL,
    PRIMARY KEY(frame_key, slot_idx, cluster_id));
CREATE TABLE IF NOT EXISTS cluster_words(
    frame_key TEXT, slot_idx INTEGER,
    cluster_id INTEGER, word TEXT, score REAL,
    PRIMARY KEY(frame_key, slot_idx, cluster_id, word));
CREATE TABLE IF NOT EXISTS coherence_log(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL, text TEXT, topical REAL, pairwise REAL,
    role REAL, flagged INTEGER);
"""


def connect(path: str | Path | None = None) -> sqlite3.Connection:
    ensure_dirs()
    target = assert_writable(Path(path) if path else COHERENCE_DB)
    target.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(target))
    db.row_factory = sqlite3.Row
    db.executescript(_SCHEMA)
    return db


def _load_totals(udb: sqlite3.Connection) -> dict:
    row = udb.execute(
        "SELECT mvalue FROM meta WHERE mkey='totals'").fetchone()
    if row is None:
        return {"tokens": 0.0, "windows": 0.0}
    try:
        return json.loads(row["mvalue"])
    except (json.JSONDecodeError, TypeError):
        return {"tokens": 0.0, "windows": 0.0}


def _ppmi_vector(udb: sqlite3.Connection, word: str, size: int = 60) -> dict[str, float]:
    row = udb.execute("SELECT freq FROM terms WHERE term=?", (word,)).fetchone()
    if row is None or float(row["freq"]) <= 0:
        return {}
    totals = _load_totals(udb)
    total_tokens = float(totals.get("tokens", 0.0))
    total_windows = float(totals.get("windows", 0.0))
    if total_tokens <= 0 or total_windows <= 0:
        return {}
    word_freq = float(row["freq"])
    rows = udb.execute(
        "SELECT b, n FROM pairs WHERE a=? ORDER BY n DESC LIMIT ?",
        (word, size)).fetchall()
    vector: dict[str, float] = {}
    for ctx_row in rows:
        ctx = str(ctx_row["b"])
        ctx_row2 = udb.execute(
            "SELECT freq FROM terms WHERE term=?", (ctx,)).fetchone()
        if ctx_row2 is None or float(ctx_row2["freq"]) <= 0:
            continue
        joint = float(ctx_row["n"]) / total_windows
        marginal = (word_freq / total_tokens) * (float(ctx_row2["freq"]) / total_tokens)
        if joint <= 0 or marginal <= 0:
            continue
        w = math.log2(joint / marginal)
        if w > 0:
            vector[ctx] = w
    return vector


def _cosine(v1: dict, v2: dict) -> float:
    shared = set(v1) & set(v2)
    if not shared:
        return 0.0
    dot = sum(v1[k] * v2[k] for k in shared)
    n1 = math.sqrt(sum(x * x for x in v1.values())) or 1.0
    n2 = math.sqrt(sum(x * x for x in v2.values())) or 1.0
    return dot / (n1 * n2)


def topical_coherence(udb: sqlite3.Connection, text: str,
                       min_vector: int = MIN_VECTOR_SIZE) -> dict:
    keys = tokens(text)
    vectors: list[dict] = []
    for k in keys:
        v = _ppmi_vector(udb, k)
        if len(v) >= min_vector:
            vectors.append(v)

    if len(vectors) < 2:
        return {"score": 0.5, "reason": "too-few-content-words",
                "content_count": len(vectors)}

    all_ctx: set[str] = set()
    for v in vectors:
        all_ctx.update(v)
    centroid: dict[str, float] = {}
    for ctx in all_ctx:
        vals = [v.get(ctx, 0.0) for v in vectors]
        centroid[ctx] = sum(vals) / len(vals)

    cosines = [_cosine(v, centroid) for v in vectors]
    mean = sum(cosines) / len(cosines)
    var = sum((c - mean) ** 2 for c in cosines) / len(cosines)
    std = math.sqrt(var)

    coherence = max(0.0, min(1.0, mean - std * 0.3))
    return {
        "score": round(coherence, 4),
        "mean": round(mean, 4),
        "std": round(std, 4),
        "content_count": len(vectors),
        "reason": "ok" if coherence >= MIN_TOPICAL_COSINE else "topical-drift",
    }


def global_ppmi_score(udb: sqlite3.Connection, text: str) -> float:
    keys = tokens(text)
    content_vecs = []
    for k in keys:
        v = _ppmi_vector(udb, k)
        if len(v) >= MIN_VECTOR_SIZE:
            content_vecs.append(v)
    if len(content_vecs) < 2:
        return 0.5

    scores: list[float] = []
    for i in range(len(content_vecs)):
        for j in range(i + 1, len(content_vecs)):
            scores.append(_cosine(content_vecs[i], content_vecs[j]))
    return round(sum(scores) / len(scores), 4) if scores else 0.5


def _get_fillers_for_slot(fdb: sqlite3.Connection, fkey: str, slot_idx: int,
                          limit: int = 100) -> list:
    try:
        tables = [r[0] for r in fdb.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
    except sqlite3.Error:
        return []
    if "frame_slots" in tables:
        rows = fdb.execute(
            "SELECT term, n FROM frame_slots "
            "WHERE fkey=? AND idx=? ORDER BY n DESC LIMIT ?",
            (fkey, slot_idx, limit)).fetchall()
        return [(str(r["term"]), float(r["n"])) for r in rows]
    return []


def cluster_roles(db: sqlite3.Connection, fdb: sqlite3.Connection,
                   udb: sqlite3.Connection,
                   limit_frames: int = 200) -> dict:
    rows = fdb.execute(
        "SELECT fkey, slots, n FROM frames ORDER BY n DESC LIMIT ?",
        (limit_frames,)).fetchall()
    started = time.time()
    clustered = 0

    for row in rows:
        fkey = str(row["fkey"])
        n_slots = int(row["slots"])
        for slot_idx in range(n_slots):
            raw = _get_fillers_for_slot(fdb, fkey, slot_idx)
            if len(raw) < ROLE_EVIDENCE_MIN:
                continue
            items = []
            for word, ev in raw:
                vec = _ppmi_vector(udb, word)
                if vec and len(vec) >= MIN_VECTOR_SIZE:
                    items.append((word, ev, vec))
            if len(items) < ROLE_EVIDENCE_MIN:
                continue

            clusters = []
            for item in items:
                word, evidence, vec = item
                best_cid = -1
                best_cos = 0.0
                for cid, cluster in enumerate(clusters):
                    for _, _, cvec in cluster:
                        cos = _cosine(vec, cvec)
                        if cos > best_cos:
                            best_cos = cos
                            best_cid = cid
                if best_cos >= ROLE_CLUSTER_THRESHOLD and best_cid >= 0:
                    clusters[best_cid].append(item)
                else:
                    clusters.append([item])

            now = time.time()
            db.execute(
                "DELETE FROM role_clusters WHERE frame_key=? AND slot_idx=?",
                (fkey, slot_idx))
            db.execute(
                "DELETE FROM cluster_words WHERE frame_key=? AND slot_idx=?",
                (fkey, slot_idx))
            for cid, members in enumerate(clusters):
                label = members[0][0] if members else ""
                db.execute(
                    "INSERT INTO role_clusters"
                    "(frame_key,slot_idx,cluster_id,label,evidence,updated_ts) "
                    "VALUES(?,?,?,?,?,?)",
                    (fkey, slot_idx, cid, label, len(members), now))
                for word, n, _ in members:
                    db.execute(
                        "INSERT INTO cluster_words"
                        "(frame_key,slot_idx,cluster_id,word,score) "
                        "VALUES(?,?,?,?,?)",
                        (fkey, slot_idx, cid, word, n))
            clustered += 1
    db.commit()
    return {"frames_clustered": clustered,
            "seconds": round(time.time() - started, 2)}


def role_coherence(db: sqlite3.Connection, udb: sqlite3.Connection,
                    text: str, fkey: str | None = None) -> dict:
    keys = tokens(text)
    if not keys or not fkey:
        return {"score": 0.5, "reason": "no-frame", "slot_scores": []}

    all_clusters = db.execute(
        "SELECT * FROM cluster_words WHERE frame_key=?", (fkey,)).fetchall()
    if not all_clusters:
        return {"score": 0.5, "reason": "no-roles-learned", "slot_scores": []}

    by_slot: dict[int, list[str]] = {}
    for c in all_clusters:
        by_slot.setdefault(int(c["slot_idx"]), []).append(str(c["word"]))

    slot_scores: list[float] = []
    for slot_idx, cluster_words in sorted(by_slot.items()):
        if slot_idx >= len(keys):
            slot_scores.append(0.5)
            continue
        filler = keys[slot_idx]
        if filler in cluster_words:
            slot_scores.append(1.0)
            continue
        filler_vec = _ppmi_vector(udb, filler)
        if not filler_vec:
            slot_scores.append(0.2)
            continue
        best = 0.0
        for cw in cluster_words:
            cw_vec = _ppmi_vector(udb, cw)
            if cw_vec:
                best = max(best, _cosine(filler_vec, cw_vec))
        slot_scores.append(best)

    if not slot_scores:
        return {"score": 0.5, "reason": "empty", "slot_scores": []}
    score = sum(slot_scores) / len(slot_scores)
    return {"score": round(score, 4), "slot_scores": slot_scores,
            "reason": "ok" if score >= 0.4 else "role-violation"}


def full_coherence(db: sqlite3.Connection, udb: sqlite3.Connection,
                    fdb: sqlite3.Connection, text: str,
                    fkey: str | None = None) -> dict:
    topical = topical_coherence(udb, text)
    pairwise = global_ppmi_score(udb, text)
    role = role_coherence(db, udb, text, fkey=fkey)

    flagged = (topical["score"] < MIN_TOPICAL_COSINE
               or pairwise < MIN_PAIR_PPMI
               or role["score"] < 0.4)

    report = {"text": text, "topical": topical, "pairwise": pairwise,
              "role": role, "flagged": flagged}
    db.execute(
        "INSERT INTO coherence_log"
        "(ts,text,topical,pairwise,role,flagged) VALUES(?,?,?,?,?,?)",
        (time.time(), text, topical["score"], pairwise,
         role["score"], int(flagged)))
    db.commit()
    return report


def stats(db: sqlite3.Connection) -> dict:
    clusters = db.execute(
        "SELECT COUNT(DISTINCT frame_key||'-'||slot_idx) AS n "
        "FROM role_clusters").fetchone()["n"]
    logged = db.execute(
        "SELECT COUNT(*) AS n FROM coherence_log").fetchone()["n"]
    flagged = db.execute(
        "SELECT COUNT(*) AS n FROM coherence_log WHERE flagged=1").fetchone()["n"]
    return {"role_slots_clustered": int(clusters or 0),
            "logged": int(logged or 0),
            "flagged": int(flagged or 0)}


def main(argv: list | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="farsi.coherence",
        description="coherence پیشرفته: topical + global PPMI + role",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    ch = sub.add_parser("check", help="بررسی coherence یک جمله")
    ch.add_argument("text")
    ch.add_argument("--fkey", default=None)

    cl = sub.add_parser("cluster-roles", help="ساخت role clusterها")
    cl.add_argument("--limit", type=int, default=200)

    sub.add_parser("stats", help="آمار")
    args = parser.parse_args(argv)

    db = connect()
    udb = usage.connect()
    fdb = frames.connect()
    try:
        if args.command == "check":
            print(full_coherence(db, udb, fdb, args.text, fkey=args.fkey))
            return 0
        if args.command == "cluster-roles":
            print(cluster_roles(db, fdb, udb, limit_frames=args.limit))
            return 0
        print(stats(db))
        return 0
    except (AgentError, OSError, ValueError, sqlite3.Error) as exc:
        print("توقف:", exc)
        return 1
    finally:
        fdb.close()
        udb.close()
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())