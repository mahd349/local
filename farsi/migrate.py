"""Idempotent schema migration for the additive layers.

CREATE TABLE IF NOT EXISTS never adds columns to an existing table, so a code
update that introduces a column silently breaks writes. This reads
PRAGMA table_info and adds only what is missing.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, r"D:\Python")
sys.path.insert(0, r"D:\project\library")

from agent.config import STATE_DIR

EXPECTED = {
    "farsi_morph.db": {"cache": {"ezafe": "INTEGER DEFAULT 0"}},
    "farsi_syntax.db": {"role_frames": {"verb_surface": "TEXT"}},
}


def migrate() -> dict:
    report: dict = {}
    for name, tables in EXPECTED.items():
        path = Path(STATE_DIR) / name
        if not path.exists():
            report[name] = "فایل موجود نیست"
            continue
        db = sqlite3.connect(str(path))
        db.execute("PRAGMA busy_timeout=60000")
        changed: list[str] = []
        try:
            for table, columns in tables.items():
                present = {str(row[1]) for row in
                           db.execute(f"PRAGMA table_info({table})")}
                if not present:
                    continue
                for column, decl in columns.items():
                    if column in present:
                        continue
                    db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
                    changed.append(f"{table}.{column}")
            db.commit()
        finally:
            db.close()
        report[name] = changed or "به‌روز"
    return report


if __name__ == "__main__":
    for name, result in migrate().items():
        print(f"{name}: {result}")