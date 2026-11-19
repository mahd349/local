"""Verified-pattern knowledge store: units, outcomes, weights, provenance."""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

from . import pystruct
from agent.config import STATE_DIR, assert_writable, ensure_dirs, get_logger

log = get_logger("knowledge")
KNOWLEDGE_DB = STATE_DIR / "knowledge.db"
START_WEIGHT = 0.5
WIN_STEP = 0.1
LOSS_STEP = 0.15

_SCHEMA = """
CREATE TABLE IF NOT EXISTS units(
    signature TEXT PRIMARY KEY, lang TEXT, kind TEXT, name TEXT, shape TEXT,
    apis TEXT, tags TEXT, flags TEXT, slots TEXT, source TEXT, origin TEXT,
    lineno INTEGER, weight REAL DEFAULT 0.5, successes INTEGER DEFAULT 0,
    failures INTEGER DEFAULT 0, verified INTEGER DEFAULT 0,
    created_ts REAL, updated_ts REAL
);
CREATE TABLE IF NOT EXISTS outcomes(
    id INTEGER PRIMARY KEY AUTOINCREMENT, signature TEXT, ts REAL,
    status TEXT, detail TEXT, duration REAL
);
CREATE INDEX IF NOT EXISTS idx_outcomes_sig ON outcomes(signature);
"""


def connect(path: str | Path | None = None) -> sqlite3.Connection:
    ensure_dirs()
    target = Path(path) if path else KNOWLEDGE_DB
    assert_writable(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(target))
    db.row_factory = sqlite3.Row
    db.executescript(_SCHEMA)
    return db


def _row_to_dict(row: sqlite3.Row) -> dict:
    item = dict(row)
    for key in ("apis", "tags", "flags", "slots"):
        item[key] = json.loads(item[key] or "[]")
    return item


def upsert_unit(db: sqlite3.Connection, unit: dict, lang: str, origin: str) -> str:
    now = time.time()
    db.execute(
        "INSERT INTO units(signature, lang, kind, name, shape, apis, tags, flags, slots, "
        "source, origin, lineno, weight, successes, failures, verified, created_ts, updated_ts) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(signature) DO UPDATE SET updated_ts=excluded.updated_ts",
        (unit["signature"], lang, unit["kind"], unit["name"], unit["shape"],
         json.dumps(unit["apis"], ensure_ascii=False),
         json.dumps(unit["tags"], ensure_ascii=False),
         json.dumps(unit["flags"], ensure_ascii=False),
         json.dumps(unit["slots"], ensure_ascii=False),
         unit["source"], origin, unit["lineno"], START_WEIGHT, 0, 0, 0, now, now),
    )
    db.commit()
    return unit["signature"]


def learn_source(db: sqlite3.Connection, lang: str, origin: str, source: str) -> list[str]:
    if lang != "python":
        return []
    return [upsert_unit(db, unit.as_dict(), lang, origin)
            for unit in pystruct.parse(source)]


def record_outcome(db: sqlite3.Connection, signature: str, status: str,
                   detail: str = "", duration: float = 0.0) -> float:
    row = db.execute("SELECT weight FROM units WHERE signature=?", (signature,)).fetchone()
    if row is None:
        raise KeyError(signature)
    weight = float(row["weight"])
    if status == "ok":
        weight = min(1.0, weight + WIN_STEP)
        db.execute("UPDATE units SET weight=?, successes=successes+1, verified=1, "
                   "updated_ts=? WHERE signature=?", (weight, time.time(), signature))
    else:
        weight = max(0.0, weight - LOSS_STEP)
        db.execute("UPDATE units SET weight=?, failures=failures+1, updated_ts=? "
                   "WHERE signature=?", (weight, time.time(), signature))
    db.execute("INSERT INTO outcomes(signature, ts, status, detail, duration) "
               "VALUES(?,?,?,?,?)", (signature, time.time(), status, detail[:500], duration))
    db.commit()
    return weight


def get(db: sqlite3.Connection, signature: str) -> dict | None:
    row = db.execute("SELECT * FROM units WHERE signature=?", (signature,)).fetchone()
    return _row_to_dict(row) if row else None


def find(db: sqlite3.Connection, tag: str | None = None, api: str | None = None,
         limit: int = 10, verified_only: bool = False) -> list[dict]:
    clauses: list[str] = []
    params: list = []
    if tag:
        clauses.append("tags LIKE ?")
        params.append(f'%"{tag}"%')
    if api:
        clauses.append("apis LIKE ?")
        params.append(f'%"{api}%')
    if verified_only:
        clauses.append("verified=1")
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    rows = db.execute(
        "SELECT * FROM units" + where +
        " ORDER BY verified DESC, weight DESC, successes DESC, updated_ts DESC LIMIT ?",
        (*params, limit),
    ).fetchall()
    return [_row_to_dict(row) for row in rows]


def stats(db: sqlite3.Connection) -> dict:
    row = db.execute("SELECT COUNT(*) AS n, SUM(verified) AS v, AVG(weight) AS w "
                     "FROM units").fetchone()
    return {"units": row["n"] or 0, "verified": row["v"] or 0,
            "avg_weight": round(row["w"] or 0.0, 3)}


def prune(db: sqlite3.Connection, min_weight: float = 0.1,
          keep_verified: bool = True) -> int:
    query = "DELETE FROM units WHERE weight < ?"
    if keep_verified:
        query += " AND verified=0"
    cursor = db.execute(query, (min_weight,))
    db.commit()
    return cursor.rowcount