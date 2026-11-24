"""Semantic role detection for Persian, learned from the corpus.
FIXED: now reads from frame_slots (the actual table name in frames.py).
"""
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

ROLES_DB = STATE_DIR / "farsi_roles.db"

ROLE_MIN_EVIDENCE = 5
ROLE_CLUSTER_THRESHOLD = 0.25
ROLE_ACCEPT_COSINE = 0.20
ROLE_MIN_FILLERS = 4

_SCHEMA = """
CREATE TABLE IF NOT EXISTS role_index(
    frame_key TEXT, slot_idx INTEGER,
    cluster_id INTEGER, label TEXT,
    member_count INTEGER, updated_ts REAL,
    PRIMARY KEY(frame_key, slot_idx, cluster_id));
CREATE TABLE IF NOT EXISTS role_members(
    frame_key TEXT, slot_idx INTEGER,
    cluster_id INTEGER, word TEXT,
    evidence REAL, vector_size INTEGER,
    PRIMARY KEY(frame_key, slot_idx, cluster_id, word));
CREATE INDEX IF NOT EXISTS idx_role_word
    ON role_members(word, frame_key, slot_idx);
CREATE TABLE IF NOT EXISTS role_verdicts(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL, frame_key TEXT, slot_idx INTEGER,
    filler TEXT, best_cosine REAL, accepted INTEGER);
"""


def connect(path: str | Path | None = None) -> sqlite3.Connection:
    ensure_dirs()
    target = assert_writable(Path(path) if path else ROLES_DB)
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


def _build_vector(udb: sqlite3.Connection, word: str, size: int = 60) -> dict[str, float]:
    freq_row = udb.execute(
        "SELECT freq FROM terms WHERE term=?", (word,)).fetchone()
    if freq_row is None or float(freq_row["freq"]) <= 0:
        return {}
    totals = _load_totals(udb)
    total_tokens = float(totals.get("tokens", 0.0))
    total_windows = float(totals.get("windows", 0.0))
    if total_tokens <= 0 or total_windows <= 0:
        return {}

    word_freq = float(freq_row["freq"])
    rows = udb.execute(
        "SELECT b, n FROM pairs WHERE a=? ORDER BY n DESC LIMIT ?",
        (word, size)).fetchall()
    vec: dict[str, float] = {}
    for r in rows:
        ctx = str(r["b"])
        ctx_row = udb.execute(
            "SELECT freq FROM terms WHERE term=?", (ctx,)).fetchone()
        if ctx_row is None or float(ctx_row["freq"]) <= 0:
            continue
        joint = float(r["n"]) / total_windows
        marg = (word_freq / total_tokens) * (float(ctx_row["freq"]) / total_tokens)
        if joint <= 0 or marg <= 0:
            continue
        w = math.log2(joint / marg)
        if w > 0:
            vec[ctx] = w
    return vec


def _cosine(v1: dict, v2: dict) -> float:
    shared = set(v1) & set(v2)
    if not shared:
        return 0.0
    dot = sum(v1[k] * v2[k] for k in shared)
    n1 = sum(x * x for x in v1.values()) ** 0.5 or 1.0
    n2 = sum(x * x for x in v2.values()) ** 0.5 or 1.0
    return dot / (n1 * n2)


def _cluster_fillers(items: list, threshold: float) -> list[list]:
    clusters: list[list] = []
    for item in items:
        word, evidence, vec = item
        if not vec:
            continue
        best_cid = -1
        best_cos = 0.0
        for cid, cluster in enumerate(clusters):
            for _, _, cvec in cluster:
                cos = _cosine(vec, cvec)
                if cos > best_cos:
                    best_cos = cos
                    best_cid = cid
        if best_cos >= threshold and best_cid >= 0:
            clusters[best_cid].append(item)
        else:
            clusters.append([item])
    return [c for c in clusters if c]


def _get_fillers_for_slot(fdb: sqlite3.Connection, fkey: str, slot_idx: int,
                          limit: int = 120) -> list:
    """FIXED: read from frame_slots, not frame_fillers."""
    try:
        tables = [r[0] for r in fdb.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
    except sqlite3.Error:
        return []

    if "frame_slots" in tables:
        rows = fdb.execute(
            "SELECT term, n FROM frame_slots "
            "WHERE fkey=? AND idx=? ORDER BY n DESC LIMIT ?",
            (fkey, slot_idx, limit),
        ).fetchall()
        return [(str(r["term"]), float(r["n"])) for r in rows]

    if "frame_fillers" in tables:
        rows = fdb.execute(
            "SELECT term, n FROM frame_fillers "
            "WHERE fkey=? AND slot_idx=? ORDER BY n DESC LIMIT ?",
            (fkey, slot_idx, limit),
        ).fetchall()
        return [(str(r["term"]), float(r["n"])) for r in rows]

    return []


def build_roles(db: sqlite3.Connection, fdb: sqlite3.Connection,
                 udb: sqlite3.Connection,
                 limit_frames: int = 300) -> dict:
    started = time.time()
    frame_rows = fdb.execute(
        "SELECT fkey, slots, n FROM frames WHERE n>=? ORDER BY n DESC LIMIT ?",
        (ROLE_MIN_EVIDENCE, limit_frames),
    ).fetchall()

    built = 0
    skipped_small = 0
    skipped_vector = 0
    now = time.time()

    for row in frame_rows:
        fkey = str(row["fkey"])
        n_slots = int(row["slots"])

        for slot_idx in range(n_slots):
            raw_fillers = _get_fillers_for_slot(fdb, fkey, slot_idx)
            if len(raw_fillers) < ROLE_MIN_FILLERS:
                skipped_small += 1
                continue

            items: list = []
            for word, ev in raw_fillers:
                vec = _build_vector(udb, word)
                if vec and len(vec) >= 20:
                    items.append((word, ev, vec))

            if len(items) < ROLE_MIN_FILLERS:
                skipped_vector += 1
                continue

            clusters = _cluster_fillers(items, ROLE_CLUSTER_THRESHOLD)

            db.execute(
                "DELETE FROM role_index WHERE frame_key=? AND slot_idx=?",
                (fkey, slot_idx),
            )
            db.execute(
                "DELETE FROM role_members WHERE frame_key=? AND slot_idx=?",
                (fkey, slot_idx),
            )
            for cid, members in enumerate(clusters):
                label = members[0][0]
                db.execute(
                    "INSERT INTO role_index"
                    "(frame_key,slot_idx,cluster_id,label,member_count,updated_ts) "
                    "VALUES(?,?,?,?,?,?)",
                    (fkey, slot_idx, cid, label, len(members), now),
                )
                for word, ev, vec in members:
                    db.execute(
                        "INSERT INTO role_members"
                        "(frame_key,slot_idx,cluster_id,word,evidence,vector_size) "
                        "VALUES(?,?,?,?,?,?)",
                        (fkey, slot_idx, cid, word, ev, len(vec)),
                    )
            built += 1
    db.commit()
    return {
        "slots_built": built,
        "frames": len(frame_rows),
        "skipped_small": skipped_small,
        "skipped_vector": skipped_vector,
        "seconds": round(time.time() - started, 2),
    }


def accepts_filler(db: sqlite3.Connection, udb: sqlite3.Connection,
                    frame_key: str, slot_idx: int, filler: str) -> dict:
    clusters = db.execute(
        "SELECT * FROM role_members WHERE frame_key=? AND slot_idx=?",
        (frame_key, slot_idx),
    ).fetchall()
    if not clusters:
        return {"accepted": True, "best_cosine": 1.0,
                "reason": "no-roles-learned-yet"}

    filler_vec = _build_vector(udb, filler)
    if not filler_vec:
        known = {str(c["word"]) for c in clusters}
        if filler in known:
            return {"accepted": True, "best_cosine": 1.0, "reason": "exact-match"}
        return {"accepted": False, "best_cosine": 0.0,
                "reason": "unknown-filler-no-vector"}

    best = 0.0
    best_word = None
    for c in clusters:
        w = str(c["word"])
        wv = _build_vector(udb, w)
        if not wv:
            continue
        cos = _cosine(filler_vec, wv)
        if cos > best:
            best = cos
            best_word = w

    accepted = best >= ROLE_ACCEPT_COSINE
    db.execute(
        "INSERT INTO role_verdicts"
        "(ts,frame_key,slot_idx,filler,best_cosine,accepted) "
        "VALUES(?,?,?,?,?,?)",
        (time.time(), frame_key, slot_idx, filler, best, int(accepted)),
    )
    db.commit()
    return {"accepted": accepted, "best_cosine": round(best, 4),
            "best_reference": best_word,
            "reason": "ok" if accepted else "role-violation"}


def validate_sentence(db: sqlite3.Connection, udb: sqlite3.Connection,
                       fdb: sqlite3.Connection, text: str,
                       frame_key: str) -> dict:
    keys = tokens(text)
    f_row = fdb.execute(
        "SELECT slots FROM frames WHERE fkey=?", (frame_key,)).fetchone()
    if f_row is None:
        return {"valid": False, "reason": "unknown-frame"}
    n_slots = int(f_row["slots"])

    verdicts: list[dict] = []
    for slot_idx in range(n_slots):
        if slot_idx >= len(keys):
            verdicts.append({
                "slot": slot_idx, "filler": "<missing>",
                "accepted": False, "reason": "missing-filler",
            })
            continue
        filler = keys[slot_idx]
        v = accepts_filler(db, udb, frame_key, slot_idx, filler)
        verdicts.append({"slot": slot_idx, "filler": filler, **v})

    all_ok = all(v.get("accepted", False) for v in verdicts)
    return {"valid": all_ok, "frame_key": frame_key, "verdicts": verdicts}


def stats(db: sqlite3.Connection) -> dict:
    slots = db.execute(
        "SELECT COUNT(DISTINCT frame_key||'-'||slot_idx) AS n "
        "FROM role_index").fetchone()["n"]
    verdicts = db.execute(
        "SELECT COUNT(*) AS n FROM role_verdicts").fetchone()["n"]
    rejected = db.execute(
        "SELECT COUNT(*) AS n FROM role_verdicts WHERE accepted=0"
    ).fetchone()["n"]
    return {"role_slots": int(slots or 0),
            "verdicts_total": int(verdicts or 0),
            "rejected": int(rejected or 0)}


def main(argv: list | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="farsi.roles",
        description="semantic role از corpus: چه کلمه‌ای برای چه slotی مناسب است",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    bd = sub.add_parser("build", help="ساخت role clusterها")
    bd.add_argument("--limit", type=int, default=300)

    vl = sub.add_parser("validate", help="بررسی یک جمله با اسکلت مشخص")
    vl.add_argument("text")
    vl.add_argument("--fkey", required=True)

    ac = sub.add_parser("accepts",
                         help="آیا این کلمه برای این slot اسکلت مناسب است؟")
    ac.add_argument("fkey")
    ac.add_argument("slot_idx", type=int)
    ac.add_argument("filler")

    sub.add_parser("stats", help="آمار")
    args = parser.parse_args(argv)

    db = connect()
    udb = usage.connect()
    fdb = frames.connect()
    try:
        if args.command == "build":
            print(build_roles(db, fdb, udb, limit_frames=args.limit))
            return 0
        if args.command == "validate":
            print(validate_sentence(db, udb, fdb, args.text, args.fkey))
            return 0
        if args.command == "accepts":
            print(accepts_filler(db, udb, args.fkey, args.slot_idx, args.filler))
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