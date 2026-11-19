"""Local Persian corpus: text plus speaker and source, never uploaded.

A flag file pauses learning so noisy or unrelated text is not absorbed.
Nothing here interprets meaning yet; that is the next layer.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
from pathlib import Path

from agent.config import STATE_DIR, assert_writable, ensure_dirs, get_logger

from .text import normalize, quality, tokens

log = get_logger("farsi.corpus")

CORPUS_DB = STATE_DIR / "farsi.db"
LEARN_OFF = STATE_DIR / "LEARN_OFF"
MAX_TEXT = 20000

_SECRET = re.compile(
    r"(?i)\b(password|passwd|pass|token|api[_-]?key|secret|authorization)\b\s*[:=]\s*\S+"
)
_LONG = re.compile(r"\b[A-Za-z0-9_\-]{32,}\b")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS docs(
id INTEGER PRIMARY KEY AUTOINCREMENT,
hash TEXT UNIQUE, source TEXT, speaker TEXT, ts REAL,
raw TEXT, keys TEXT, quality REAL, weight REAL DEFAULT 1.0
);
CREATE INDEX IF NOT EXISTS idx_docs_source ON docs(source, ts);
"""


def redact(text: str) -> str:
    """Best-effort masking of obvious secrets before anything is stored."""
    value = _SECRET.sub(lambda m: m.group(1) + ": ***", str(text or ""))
    return _LONG.sub("***", value)


def learning_enabled() -> bool:
    return not LEARN_OFF.exists()


def set_learning(enabled: bool) -> None:
    ensure_dirs()
    if enabled:
        LEARN_OFF.unlink(missing_ok=True)
    else:
        LEARN_OFF.write_text("off", encoding="utf-8")


def connect(path: str | Path | None = None) -> sqlite3.Connection:
    ensure_dirs()
    target = assert_writable(Path(path) if path else CORPUS_DB)
    target.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(target))
    db.row_factory = sqlite3.Row
    db.executescript(_SCHEMA)
    return db


def add_text(db: sqlite3.Connection, text: str, *, source: str = "user",
             speaker: str = "me", ts: float | None = None,
             use_redaction: bool = True) -> dict:
    if not learning_enabled():
        return {"stored": False, "reason": "learn-off"}
    raw = str(text or "").strip()[:MAX_TEXT]
    if not raw:
        return {"stored": False, "reason": "empty"}
    if use_redaction:
        raw = redact(raw)
    keys = " ".join(tokens(raw))
    if not keys:
        return {"stored": False, "reason": "no-tokens"}
    digest = hashlib.sha1(normalize(raw).encode("utf-8")).hexdigest()
    existing = db.execute("SELECT id FROM docs WHERE hash=?", (digest,)).fetchone()
    if existing:
        return {"stored": False, "reason": "duplicate", "id": existing["id"]}
    cursor = db.execute(
        "INSERT INTO docs(hash, source, speaker, ts, raw, keys, quality) "
        "VALUES(?,?,?,?,?,?,?)",
        (digest, source, speaker, float(ts if ts is not None else time.time()),
         raw, keys, quality(raw)),
    )
    db.commit()
    return {"stored": True, "id": int(cursor.lastrowid), "tokens": len(keys.split())}


def _messages(node):
    """Yield (role, content) from any chat export shape, without assuming a schema."""
    if isinstance(node, dict):
        role, content = node.get("role"), node.get("content")
        if isinstance(role, str) and isinstance(content, str) and content.strip():
            yield role, content
        for value in node.values():
            yield from _messages(value)
    elif isinstance(node, list):
        for item in node:
            yield from _messages(item)


def add_json(db: sqlite3.Connection, path: str | Path, *, source: str = "archive") -> dict:
    data = json.loads(Path(path).read_text(encoding="utf-8-sig", errors="replace"))
    stats = {"seen": 0, "stored": 0, "skipped": 0}
    for role, content in _messages(data):
        stats["seen"] += 1
        result = add_text(db, content, source=source, speaker=role)
        stats["stored" if result["stored"] else "skipped"] += 1
    return stats


def stats(db: sqlite3.Connection) -> dict:
    row = db.execute(
        "SELECT COUNT(*) AS n, AVG(quality) AS q, "
        "SUM(LENGTH(keys) - LENGTH(REPLACE(keys, ' ', '')) + 1) AS t FROM docs"
    ).fetchone()
    return {"docs": row["n"] or 0, "tokens": row["t"] or 0,
            "avg_quality": round(row["q"] or 0.0, 3),
            "learning": "on" if learning_enabled() else "off"}


def recent(db: sqlite3.Connection, limit: int = 10) -> list[dict]:
    rows = db.execute(
        "SELECT id, source, speaker, ts, raw, quality FROM docs "
        "ORDER BY id DESC LIMIT ?", (int(limit),)
    ).fetchall()
    return [dict(row) for row in rows]