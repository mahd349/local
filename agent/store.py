"""SQLite state shared by the engine, the bridge and the AHK front-end."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from typing import Any

from .config import DB_PATH, ensure_dirs

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    kind TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS commands (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    kind TEXT NOT NULL,
    payload TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending'
);
CREATE INDEX IF NOT EXISTS idx_commands_state ON commands(state, id);
CREATE TABLE IF NOT EXISTS approvals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    kind TEXT NOT NULL,
    summary TEXT NOT NULL,
    payload TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending',
    decided_ts REAL
);
CREATE TABLE IF NOT EXISTS missions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    path TEXT NOT NULL,
    spec TEXT NOT NULL,
    created_ts REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mission_id INTEGER,
    started_ts REAL NOT NULL,
    finished_ts REAL,
    status TEXT NOT NULL DEFAULT 'running',
    report TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS steps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL,
    idx INTEGER NOT NULL,
    action TEXT NOT NULL,
    target TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '',
    ts REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_steps_run ON steps(run_id, idx);
CREATE TABLE IF NOT EXISTS selectors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    origin TEXT NOT NULL,
    key TEXT NOT NULL,
    candidates TEXT NOT NULL,
    hits INTEGER NOT NULL DEFAULT 0,
    misses INTEGER NOT NULL DEFAULT 0,
    updated_ts REAL NOT NULL,
    UNIQUE(origin, key)
);
CREATE TABLE IF NOT EXISTS macros (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    params TEXT NOT NULL,
    steps TEXT NOT NULL,
    created_ts REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS captures (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER,
    ts REAL NOT NULL,
    method TEXT NOT NULL,
    url TEXT NOT NULL,
    status INTEGER NOT NULL DEFAULT 0,
    mime TEXT NOT NULL DEFAULT '',
    size INTEGER NOT NULL DEFAULT 0,
    headers TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def _loads(raw: str, fallback: Any) -> Any:
    try:
        return json.loads(raw)
    except ValueError:
        return fallback


class Store:
    """Autocommit connection guarded by a lock; bridge and engine share it."""

    def __init__(self, path=DB_PATH):
        ensure_dirs()
        self._lock = threading.RLock()
        self._db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=NORMAL")
        with self._lock:
            self._db.executescript(_SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def _write(self, sql: str, args: tuple = ()) -> int:
        with self._lock:
            cur = self._db.execute(sql, args)
            return int(cur.lastrowid)

    def _read(self, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._db.execute(sql, args).fetchall()

    # ---- events (AHK polls these by id) ----
    def add_event(self, kind: str, payload: dict | None = None) -> int:
        return self._write(
            "INSERT INTO events(ts, kind, payload) VALUES(?,?,?)",
            (time.time(), kind, _dumps(payload or {})),
        )

    def events_since(self, since: int, limit: int = 200) -> list[dict]:
        rows = self._read(
            "SELECT id, ts, kind, payload FROM events WHERE id > ? ORDER BY id LIMIT ?",
            (since, limit),
        )
        return [
            {"id": r["id"], "ts": r["ts"], "kind": r["kind"], "payload": _loads(r["payload"], {})}
            for r in rows
        ]

    # ---- commands (AHK -> engine) ----
    def add_command(self, kind: str, payload: dict | None = None) -> int:
        return self._write(
            "INSERT INTO commands(ts, kind, payload) VALUES(?,?,?)",
            (time.time(), kind, _dumps(payload or {})),
        )

    def next_command(self) -> dict | None:
        with self._lock:
            row = self._db.execute(
                "SELECT id, kind, payload FROM commands WHERE state='pending' ORDER BY id LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            self._db.execute("UPDATE commands SET state='taken' WHERE id=?", (row["id"],))
            return {"id": row["id"], "kind": row["kind"], "payload": _loads(row["payload"], {})}

    # ---- approvals ----
    def create_approval(self, kind: str, summary: str, payload: dict | None = None) -> int:
        return self._write(
            "INSERT INTO approvals(ts, kind, summary, payload) VALUES(?,?,?,?)",
            (time.time(), kind, summary, _dumps(payload or {})),
        )

    def approval(self, approval_id: int) -> dict | None:
        rows = self._read(
            "SELECT id, kind, summary, payload, state, ts FROM approvals WHERE id=?",
            (approval_id,),
        )
        if not rows:
            return None
        r = rows[0]
        return {
            "id": r["id"], "kind": r["kind"], "summary": r["summary"],
            "payload": _loads(r["payload"], {}), "state": r["state"], "ts": r["ts"],
        }

    def pending_approvals(self) -> list[dict]:
        rows = self._read(
            "SELECT id FROM approvals WHERE state='pending' ORDER BY id"
        )
        return [self.approval(r["id"]) for r in rows]

    def decide_approval(self, approval_id: int, granted: bool) -> bool:
        with self._lock:
            cur = self._db.execute(
                "UPDATE approvals SET state=?, decided_ts=? WHERE id=? AND state='pending'",
                ("granted" if granted else "denied", time.time(), approval_id),
            )
            return cur.rowcount > 0

    # ---- missions / runs / steps ----
    def create_mission(self, name: str, path: str, spec: dict) -> int:
        return self._write(
            "INSERT INTO missions(name, path, spec, created_ts) VALUES(?,?,?,?)",
            (name, path, _dumps(spec), time.time()),
        )

    def create_run(self, mission_id: int | None) -> int:
        return self._write(
            "INSERT INTO runs(mission_id, started_ts) VALUES(?,?)", (mission_id, time.time())
        )

    def finish_run(self, run_id: int, status: str, report: dict) -> None:
        self._write(
            "UPDATE runs SET finished_ts=?, status=?, report=? WHERE id=?",
            (time.time(), status, _dumps(report), run_id),
        )

    def add_step(self, run_id: int, idx: int, action: str, target: str,
                 status: str, detail: str = "") -> int:
        return self._write(
            "INSERT INTO steps(run_id, idx, action, target, status, detail, ts) "
            "VALUES(?,?,?,?,?,?,?)",
            (run_id, idx, action, target, status, detail, time.time()),
        )

    # ---- self-healing selector memory ----
    def _selector_entries(self, origin: str, key: str) -> list[dict]:
        rows = self._read(
            "SELECT candidates FROM selectors WHERE origin=? AND key=?", (origin, key)
        )
        if not rows:
            return []
        entries = []
        for item in _loads(rows[0]["candidates"], []):
            if isinstance(item, str):
                entries.append({"css": item, "misses": 0})
            elif isinstance(item, dict) and item.get("css"):
                entries.append({"css": item["css"], "misses": int(item.get("misses", 0))})
        return entries

    def _write_entries(self, origin: str, key: str, entries: list[dict]) -> None:
        self._write(
            "INSERT INTO selectors(origin, key, candidates, updated_ts) VALUES(?,?,?,?) "
            "ON CONFLICT(origin, key) DO UPDATE SET candidates=excluded.candidates, "
            "updated_ts=excluded.updated_ts",
            (origin, key, _dumps(entries), time.time()),
        )

    def get_selectors(self, origin: str, key: str) -> list[str]:
        return [e["css"] for e in self._selector_entries(origin, key)]

    def save_selectors(self, origin: str, key: str, candidates: list[str]) -> None:
        previous = {e["css"]: e["misses"] for e in self._selector_entries(origin, key)}
        self._write_entries(
            origin, key,
            [{"css": css, "misses": previous.get(css, 0)} for css in candidates],
        )

    def bump_selector(self, origin: str, key: str, hit: bool) -> None:
        column = "hits" if hit else "misses"
        self._write(
            f"UPDATE selectors SET {column}={column}+1, updated_ts=? WHERE origin=? AND key=?",
            (time.time(), origin, key),
        )

    def note_candidate(self, origin: str, key: str, css: str, hit: bool) -> None:
        """A hit clears the miss streak; a miss increments it."""
        entries = self._selector_entries(origin, key)
        for entry in entries:
            if entry["css"] == css:
                entry["misses"] = 0 if hit else entry["misses"] + 1
                break
        else:
            entries.append({"css": css, "misses": 0 if hit else 1})
        self._write_entries(origin, key, entries)

    def prune_selectors(self, origin: str, key: str, max_misses: int = 3) -> list[str]:
        entries = self._selector_entries(origin, key)
        kept = [e for e in entries if e["misses"] < max_misses]
        if len(kept) != len(entries):
            self._write_entries(origin, key, kept)
        return [e["css"] for e in kept]

    # ---- learned macros ----
    def save_macro(self, name: str, params: list, steps: list) -> int:
        return self._write(
            "INSERT INTO macros(name, params, steps, created_ts) VALUES(?,?,?,?) "
            "ON CONFLICT(name) DO UPDATE SET params=excluded.params, steps=excluded.steps",
            (name, _dumps(params), _dumps(steps), time.time()),
        )

    def get_macro(self, name: str) -> dict | None:
        rows = self._read("SELECT name, params, steps FROM macros WHERE name=?", (name,))
        if not rows:
            return None
        r = rows[0]
        return {"name": r["name"], "params": _loads(r["params"], []), "steps": _loads(r["steps"], [])}

    def list_macros(self) -> list[str]:
        return [r["name"] for r in self._read("SELECT name FROM macros ORDER BY name")]

    # ---- network capture metadata ----
    def add_capture(self, run_id: int | None, method: str, url: str, status: int,
                    mime: str, size: int, headers: dict) -> int:
        return self._write(
            "INSERT INTO captures(run_id, ts, method, url, status, mime, size, headers) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (run_id, time.time(), method, url, status, mime, size, _dumps(headers)),
        )

    # ---- misc ----
    def kv_set(self, key: str, value: Any) -> None:
        self._write(
            "INSERT INTO kv(key, value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, _dumps(value)),
        )

    def kv_get(self, key: str, fallback: Any = None) -> Any:
        rows = self._read("SELECT value FROM kv WHERE key=?", (key,))
        return _loads(rows[0]["value"], fallback) if rows else fallback