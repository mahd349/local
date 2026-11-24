"""Capability cards: typed, verified building blocks that the planner composes.

A card is not a code copy. It declares what it takes, what it gives back and
what it needs, so different cards can be wired into programs never seen before.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import time
from pathlib import Path

from agent.config import STATE_DIR, assert_writable, ensure_dirs, get_logger
from agent.errors import AgentError

log = get_logger("capability")

CAPS_DB = STATE_DIR / "capabilities.db"
CAPS_DIR = STATE_DIR / "caps"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS caps(
key TEXT PRIMARY KEY, title TEXT, goals TEXT, inputs TEXT, outputs TEXT,
packages TEXT, body TEXT, effects TEXT, context TEXT, exclude TEXT,
priority INTEGER DEFAULT 10, weight REAL DEFAULT 0.5, verified INTEGER DEFAULT 0,
origin TEXT, created_ts REAL, updated_ts REAL);
"""

SEEDS: list[dict] = [
    {
        "key": "text.literal", "title": "متن آماده",
        "goals": ["متن", "text", "ورودی", "input"],
        "inputs": [{"name": "value", "type": "str", "default": "سلام"}],
        "outputs": [{"name": "text", "type": "str"}],
        "priority": 0,
        "body": "def cap_literal(value):\n    return str(value)\n",
    },
    {
        "key": "text.sha256", "title": "ساخت هش متن",
        "goals": ["هش", "hash", "sha", "امضا"],
        "inputs": [{"name": "text", "type": "str"}],
        "outputs": [{"name": "digest", "type": "str"}],
        "body": ("def cap_sha256(text):\n"
                 "    import hashlib\n"
                 "    return hashlib.sha256(str(text).encode('utf-8')).hexdigest()\n"),
    },
    {
        "key": "text.write", "title": "ذخیرهٔ متن در فایل",
        "goals": ["ذخیره", "save", "نوشتن", "فایل"],
        "inputs": [{"name": "text", "type": "str"},
                   {"name": "path", "type": "path",
                    "default": "D:/Work/agent-state/generated/output.txt"}],
        "outputs": [{"name": "path", "type": "path"}],
        "effects": ["file-write"],
        "body": ("def cap_write(text, path):\n"
                 "    from pathlib import Path\n"
                 "    target = Path(path)\n"
                 "    target.parent.mkdir(parents=True, exist_ok=True)\n"
                 "    target.write_text(str(text), encoding='utf-8')\n"
                 "    return str(target)\n"),
    },
    {
        "key": "text.read", "title": "خواندن متن از فایل",
        "goals": ["خواندن", "read", "بازکردن"],
        "inputs": [{"name": "path", "type": "path"}],
        "outputs": [{"name": "text", "type": "str"}],
        "body": ("def cap_read(path):\n"
                 "    from pathlib import Path\n"
                 "    return Path(path).read_text(encoding='utf-8', errors='replace')\n"),
    },
    {
        "key": "file.link", "title": "ساخت لینک محلی فایل",
        "goals": ["لینک", "link", "آدرس"],
        "inputs": [{"name": "path", "type": "path"}],
        "outputs": [{"name": "url", "type": "url"}],
        "body": ("def cap_link(path):\n"
                 "    from pathlib import Path\n"
                 "    return Path(path).resolve().as_uri()\n"),
    },
    {
        "key": "gui.show_text", "title": "نمایش متن در پنجره",
        "goals": ["نمایش", "نشون", "show", "پنجره", "window"],
        "inputs": [{"name": "text", "type": "str"}],
        "outputs": [],
        "effects": ["gui"],
        "exclude": ["عکس", "تصویر", "image", "کیو", "qr"],
        "body": ("def cap_show_text(text):\n"
                 "    import tkinter as tk\n"
                 "    root = tk.Tk()\n"
                 "    root.title('خروجی ایجنت')\n"
                 "    root.geometry('520x260')\n"
                 "    box = tk.Label(root, text=str(text), wraplength=480,\n"
                 "                   justify='left', anchor='nw')\n"
                 "    box.pack(fill='both', expand=True, padx=10, pady=10)\n"
                 "    tk.Button(root, text='بستن', command=root.destroy).pack(pady=6)\n"
                 "    root.mainloop()\n"),
    },
    {
        "key": "gui.show_image", "title": "نمایش عکس در پنجره",
        "goals": ["عکس", "تصویر", "image", "نشون", "نمایش"],
        "inputs": [{"name": "path", "type": "path"}],
        "outputs": [],
        "effects": ["gui"],
        "context": ["عکس", "تصویر", "image", "کیو", "qr"],
        "body": ("def cap_show_image(path):\n"
                 "    import tkinter as tk\n"
                 "    root = tk.Tk()\n"
                 "    root.title('عکس')\n"
                 "    image = tk.PhotoImage(file=str(path))\n"
                 "    tk.Label(root, image=image).pack(padx=10, pady=10)\n"
                 "    tk.Button(root, text='بستن', command=root.destroy).pack(pady=6)\n"
                 "    root.image = image\n"
                 "    root.mainloop()\n"),
    },
    {
        "key": "qr.make", "title": "ساخت کد QR از متن",
        "goals": ["qr", "کیو آر", "کیوآر", "کیو", "بارکد"],
        "inputs": [{"name": "text", "type": "str"},
                   {"name": "path", "type": "path",
                    "default": "D:/Work/agent-state/generated/qr.ppm"}],
        "outputs": [{"name": "path", "type": "path"}],
        "packages": ["qrcode"],
        "effects": ["file-write"],
        "body": ("def cap_qr(text, path):\n"
                 "    import qrcode\n"
                 "    from qrcode.constants import ERROR_CORRECT_M\n"
                 "    from pathlib import Path\n"
                 "    code = qrcode.QRCode(border=4, error_correction=ERROR_CORRECT_M)\n"
                 "    code.add_data(str(text))\n"
                 "    code.make(fit=True)\n"
                 "    matrix = code.get_matrix()\n"
                 "    scale = 8\n"
                 "    size = len(matrix) * scale\n"
                 "    rows = []\n"
                 "    for row in matrix:\n"
                 "        wide = b''\n"
                 "        for cell in row:\n"
                 "            tone = 0 if cell else 255\n"
                 "            wide += bytes([tone, tone, tone]) * scale\n"
                 "        for _ in range(scale):\n"
                 "            rows.append(wide)\n"
                 "    target = Path(path)\n"
                 "    target.parent.mkdir(parents=True, exist_ok=True)\n"
                 "    header = ('P6\\n%d %d\\n255\\n' % (size, size)).encode('ascii')\n"
                 "    target.write_bytes(header + b''.join(rows))\n"
                 "    return str(target)\n"),
    },
]

_FIELDS = ("goals", "inputs", "outputs", "packages", "effects", "context", "exclude")


def connect(path: str | Path | None = None) -> sqlite3.Connection:
    ensure_dirs()
    target = Path(path) if path else CAPS_DB
    assert_writable(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(target))
    db.row_factory = sqlite3.Row
    db.executescript(_SCHEMA)
    return db


def _row(row: sqlite3.Row) -> dict:
    item = dict(row)
    for key in _FIELDS:
        item[key] = json.loads(item[key] or "[]")
    return item


def normalize_card(raw: dict) -> dict:
    if not isinstance(raw, dict) or not raw.get("key") or not raw.get("body"):
        raise AgentError("کارت قابلیت باید key و body داشته باشد")
    card = {
        "key": str(raw["key"]), "title": str(raw.get("title") or raw["key"]),
        "body": str(raw["body"]), "priority": int(raw.get("priority", 10)),
        "origin": str(raw.get("origin", "seed")),
    }
    for key in _FIELDS:
        value = raw.get(key) or []
        if not isinstance(value, list):
            raise AgentError(f"فیلد {key} باید فهرست باشد")
        card[key] = value
    return card


def upsert(db: sqlite3.Connection, raw: dict) -> str:
    card = normalize_card(raw)
    now = time.time()
    values = [card["key"], card["title"]]
    values += [json.dumps(card[key], ensure_ascii=False) for key in _FIELDS]
    values += [card["body"], card["priority"], card["origin"], now, now]
    db.execute(
        "INSERT INTO caps(key, title, goals, inputs, outputs, packages, effects, "
        "context, exclude, body, priority, origin, created_ts, updated_ts) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(key) DO UPDATE SET title=excluded.title, goals=excluded.goals, "
        "inputs=excluded.inputs, outputs=excluded.outputs, packages=excluded.packages, "
        "effects=excluded.effects, context=excluded.context, exclude=excluded.exclude, "
        "body=excluded.body, priority=excluded.priority, updated_ts=excluded.updated_ts",
        values,
    )
    db.commit()
    return card["key"]


def seed(db: sqlite3.Connection) -> int:
    for raw in SEEDS:
        upsert(db, raw)
    return len(SEEDS)


def load_dir(db: sqlite3.Connection, folder: str | Path | None = None) -> int:
    source = Path(folder) if folder else CAPS_DIR
    if not source.exists():
        return 0
    count = 0
    for path in sorted(source.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except ValueError as exc:
            log.warning("کارت نامعتبر %s: %s", path.name, exc)
            continue
        for raw in (data if isinstance(data, list) else [data]):
            raw.setdefault("origin", str(path))
            try:
                upsert(db, raw)
                count += 1
            except AgentError as exc:
                log.warning("کارت رد شد %s: %s", path.name, exc)
    return count


def all_caps(db: sqlite3.Connection) -> list[dict]:
    rows = db.execute(
        "SELECT * FROM caps ORDER BY priority, verified DESC, weight DESC, key"
    ).fetchall()
    return [_row(row) for row in rows]


def get(db: sqlite3.Connection, key: str) -> dict | None:
    row = db.execute("SELECT * FROM caps WHERE key=?", (key,)).fetchone()
    return _row(row) if row else None


def record(db: sqlite3.Connection, key: str, ok: bool) -> float:
    row = db.execute("SELECT weight FROM caps WHERE key=?", (key,)).fetchone()
    if row is None:
        raise KeyError(key)
    weight = float(row["weight"])
    weight = min(1.0, weight + 0.1) if ok else max(0.0, weight - 0.15)
    db.execute(
        "UPDATE caps SET weight=?, verified=?, updated_ts=? WHERE key=?",
        (weight, 1 if ok else 0, time.time(), key),
    )
    db.commit()
    return weight


def prepare() -> sqlite3.Connection:
    db = connect()
    if not db.execute("SELECT COUNT(*) AS n FROM caps").fetchone()["n"]:
        seed(db)
    load_dir(db)
    return db


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agent.capability", description="کارت‌های قابلیت")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list", help="فهرست قابلیت‌ها")
    sub.add_parser("seed", help="بازنشانی قابلیت‌های پایه")
    args = parser.parse_args(argv)
    db = prepare()
    try:
        if args.command == "seed":
            print("قابلیت‌های پایه:", seed(db))
            return 0
        for card in all_caps(db):
            needs = ", ".join(card["packages"]) or "بدون نصب"
            ins = ", ".join(f"{s['name']}:{s['type']}" for s in card["inputs"]) or "-"
            outs = ", ".join(s["type"] for s in card["outputs"]) or "-"
            print(f"{card['key']:16} {card['title']}\n"
                  f"    ورودی: {ins} | خروجی: {outs} | نیاز: {needs} "
                  f"| وزن {card['weight']:.2f}")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())