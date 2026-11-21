"""Intent layer: map text to a route by evidence, never by fiat.

Routes are scored against stored examples, optionally expanded with learned
neighbours from the usage engine. Unmatched text becomes conversation, so no
tool can run by accident. Execution needs an explicit request plus approval.
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

from agent.config import STATE_DIR, assert_writable, child_env, ensure_dirs, get_logger
from agent.errors import AgentError

from . import corpus, usage
from .text import normalize, tokens

log = get_logger("farsi.intent")

INTENT_DB = STATE_DIR / "farsi_intent.db"
ACTION_MIN = 0.55
NEAR_MIN = 0.32
NEIGHBOR_MIN_SCORE = 0.5
NEIGHBOR_MIN_CONF = 0.2
CREATE_NO_WINDOW = 0x08000000

_PATH = re.compile(r"[A-Za-z]:[\\/][^\s\"'<>|]+")
_ARG_TEXT = re.compile(r"[A-Za-z]:[\\/][^\s\"'<>|]+|https?://\S+")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS routes(
key TEXT PRIMARY KEY, title TEXT, kind TEXT, command TEXT, slots TEXT,
weight REAL DEFAULT 0.5, hits INTEGER DEFAULT 0, misses INTEGER DEFAULT 0,
origin TEXT, created_ts REAL, updated_ts REAL);
CREATE TABLE IF NOT EXISTS examples(
id INTEGER PRIMARY KEY AUTOINCREMENT, route TEXT, text TEXT, keys TEXT,
weight REAL DEFAULT 1.0, origin TEXT, ts REAL);
CREATE INDEX IF NOT EXISTS idx_examples_route ON examples(route);
CREATE TABLE IF NOT EXISTS feedback(
id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, route TEXT, text TEXT,
accepted INTEGER, detail TEXT);
"""

SEED_ROUTES = [
    {"key": "learn.python", "title": "یادگیری پایتون", "kind": "action",
     "command": ["brain", "lesson", "next"], "slots": [],
     "examples": ["برو پایتون یاد بگیر", "پایتون یاد بگیر",
                  "یه درس پایتون بگیر", "برو از کویین پایتون یاد بگیر"]},
    {"key": "build.program", "title": "ساخت برنامه", "kind": "action",
     "command": ["brain", "planner", "{request}", "--build"], "slots": ["request"],
     "examples": ["یه برنامه بساز", "برنامه بساز که", "یک برنامه بنویس"]},
    {"key": "scan.code", "title": "اسکن کدها", "kind": "action",
     "command": ["brain", "ingest", "scan", "--root", "{path}", "--verify", "20"],
     "slots": ["path"],
     "examples": ["کدها را اسکن کن", "این پوشه را یاد بگیر", "فایل های پایتون را بخوان"]},
    {"key": "search.files", "title": "جست‌وجوی فایل", "kind": "action",
     "command": ["agent", "index", "search", "{query}"], "slots": ["query"],
     "examples": ["دنبال فایل بگرد", "توی لپ تاپم جستجو کن", "فایل را پیدا کن"]},
    {"key": "fix.code", "title": "تعمیر کد", "kind": "action",
     "command": ["agent", "code", "fix", "{path}"], "slots": ["path"],
     "examples": ["این فایل خطا دارد درستش کن", "کد را تعمیر کن", "این فایل را اصلاح کن"]},
    {"key": "promote.knowledge", "title": "ارتقای دانش", "kind": "action",
     "command": ["brain", "bridge", "promote", "--limit", "20"], "slots": [],
     "examples": ["الگوها را ارتقا بده", "دانش را قابل استفاده کن"]},
    {"key": "talk.feeling", "title": "گفت‌وگوی احساسی", "kind": "conversation",
     "command": [], "slots": [],
     "examples": ["دلگیرم", "حالم خوب نیست", "خسته ام", "حوصله ندارم"]},
    {"key": "talk.greeting", "title": "احوالپرسی", "kind": "conversation",
     "command": [], "slots": [],
     "examples": ["سلام", "چطوری", "خوبی", "درود"]},
    {"key": "talk.explain", "title": "پرسش توضیحی", "kind": "conversation",
     "command": [], "slots": [],
     "examples": ["چرا آسمان آبی است", "این چیست", "توضیح بده", "معنی این چیست"]},
]


def connect(path: str | Path | None = None) -> sqlite3.Connection:
    ensure_dirs()
    target = assert_writable(Path(path) if path else INTENT_DB)
    target.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(target))
    db.row_factory = sqlite3.Row
    db.executescript(_SCHEMA)
    return db


def seed(db: sqlite3.Connection) -> int:
    now = time.time()
    for route in SEED_ROUTES:
        db.execute(
            "INSERT INTO routes(key,title,kind,command,slots,origin,created_ts,updated_ts) "
            "VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(key) DO UPDATE SET title=excluded.title",
            (route["key"], route["title"], route["kind"],
             json.dumps(route["command"], ensure_ascii=False),
             json.dumps(route["slots"], ensure_ascii=False), "seed", now, now),
        )
        for text in route["examples"]:
            keys = " ".join(tokens(text))
            exists = db.execute(
                "SELECT id FROM examples WHERE route=? AND keys=?", (route["key"], keys)
            ).fetchone()
            if exists is None:
                db.execute(
                    "INSERT INTO examples(route,text,keys,origin,ts) VALUES(?,?,?,?,?)",
                    (route["key"], text, keys, "seed", now),
                )
    db.commit()
    return len(SEED_ROUTES)


def prepare(path: str | Path | None = None) -> sqlite3.Connection:
    db = connect(path)
    if not db.execute("SELECT COUNT(*) AS n FROM routes").fetchone()["n"]:
        seed(db)
    return db


def routes(db: sqlite3.Connection) -> list[dict]:
    out = []
    for row in db.execute("SELECT * FROM routes ORDER BY kind, key").fetchall():
        item = dict(row)
        item["command"] = json.loads(item["command"] or "[]")
        item["slots"] = json.loads(item["slots"] or "[]")
        item["examples"] = [
            dict(r) for r in db.execute(
                "SELECT id,text,weight,origin FROM examples WHERE route=? ORDER BY id",
                (item["key"],)).fetchall()
        ]
        out.append(item)
    return out


def _expansion(udb, query_keys: list[str]) -> set[str]:
    """Learned neighbours only, and only when the usage engine is confident."""
    if udb is None:
        return set()
    gained: set[str] = set()
    for word in query_keys:
        try:
            near = usage.neighbors(udb, word, limit=5)
        except (sqlite3.Error, KeyError, ValueError):
            continue
        for item in near:
            if (item["score"] >= NEIGHBOR_MIN_SCORE
                    and item["confidence"] >= NEIGHBOR_MIN_CONF):
                gained.add(item["term"])
    return gained - set(query_keys)


def _similarity(query: set[str], example: set[str], gained: set[str],
                tolerant: bool = False) -> float:
    if not query or not example:
        return 0.0
    direct = len(query & example)
    extra = len((gained & example) - query)
    hit = direct + 0.5 * extra
    if hit <= 0:
        return 0.0
    coverage = min(1.0, hit / len(example))
    precision = min(1.0, hit / len(query))
    if tolerant:
        # A free-text payload route expects extra words; favour coverage.
        return 5 * coverage * precision / (4 * precision + coverage)
    return 2 * coverage * precision / (coverage + precision)


def _slots(text: str, route: dict) -> tuple[dict, list[str]]:
    values: dict[str, str] = {}
    missing: list[str] = []
    found = _PATH.search(text)
    for slot in route["slots"]:
        if slot == "path":
            if found:
                values[slot] = found.group(0).rstrip(".,؛")
            else:
                missing.append(slot)
        else:
            values[slot] = text.strip()
    return values, missing


def argv_for(route: dict, values: dict) -> list[str]:
    if not route["command"]:
        return []
    package, rest = route["command"][0], route["command"][1:]
    argv = [sys.executable, "-m", package]
    for piece in rest:
        if piece.startswith("{") and piece.endswith("}"):
            argv.append(str(values.get(piece[1:-1], "")))
        else:
            argv.append(piece)
    return argv


def classify(text: str, db: sqlite3.Connection, udb=None, *,
             learn: bool = False) -> dict:
    raw = str(text or "").strip()
    # Arguments are payload, not intent words, so they must not dilute matching.
    query_list = tokens(_ARG_TEXT.sub(" ", raw))
    query = set(query_list)
    gained = _expansion(udb, query_list)

    best = None
    for route in routes(db):
        tolerant = bool({"request", "query"} & set(route["slots"]))
        for example in route["examples"]:
            keys = set(tokens(example["text"]))
            score = _similarity(query, keys, gained, tolerant) * float(example["weight"])
            if best is None or score > best["score"]:
                best = {"route": route, "score": round(score, 4),
                        "example": example["text"]}

    result = {"text": raw, "kind": "conversation", "route": None, "title": "",
              "score": best["score"] if best else 0.0,
              "example": best["example"] if best else "",
              "values": {}, "missing": [], "argv": [], "note": ""}

    if best and best["score"] >= ACTION_MIN:
        route = best["route"]
        result.update(kind=route["kind"], route=route["key"], title=route["title"])
        if route["kind"] == "action":
            values, missing = _slots(raw, route)
            result["values"] = values
            result["missing"] = missing
            result["argv"] = argv_for(route, values) if not missing else []
            result["note"] = "ورودی لازم است: " + ", ".join(missing) if missing else ""
    elif best and best["score"] >= NEAR_MIN:
        result.update(kind="unknown", route=best["route"]["key"],
                      title=best["route"]["title"],
                      note="نزدیک ولی نامطمئن؛ با teach تأییدش کن")
    else:
        result["note"] = "گفت‌وگو؛ هیچ ابزاری اجرا نمی‌شود"

    if learn:
        corpus.add_text(corpus.connect(), raw, source="intent", speaker="me")
    return result


def teach(db: sqlite3.Connection, route_key: str, text: str) -> dict:
    if db.execute("SELECT key FROM routes WHERE key=?", (route_key,)).fetchone() is None:
        raise AgentError(f"مسیر ناشناس: {route_key}")
    keys = " ".join(tokens(text))
    if not keys:
        raise AgentError("متن نمونه خالی است")
    db.execute("INSERT INTO examples(route,text,keys,origin,ts) VALUES(?,?,?,?,?)",
               (route_key, str(text).strip(), keys, "user", time.time()))
    db.commit()
    return {"route": route_key, "text": str(text).strip()}


def feedback(db: sqlite3.Connection, route_key: str, text: str, accepted: bool) -> float:
    row = db.execute("SELECT weight FROM routes WHERE key=?", (route_key,)).fetchone()
    if row is None:
        raise AgentError(f"مسیر ناشناس: {route_key}")
    weight = float(row["weight"])
    weight = min(1.0, weight + 0.1) if accepted else max(0.0, weight - 0.15)
    db.execute(
        "UPDATE routes SET weight=?, hits=hits+?, misses=misses+?, updated_ts=? WHERE key=?",
        (weight, 1 if accepted else 0, 0 if accepted else 1, time.time(), route_key),
    )
    db.execute("INSERT INTO feedback(ts,route,text,accepted) VALUES(?,?,?,?)",
               (time.time(), route_key, str(text)[:300], 1 if accepted else 0))
    db.commit()
    return weight


def run(text: str, db: sqlite3.Connection, udb, notifier, *, runner=None) -> dict:
    decision = classify(text, db, udb)
    if decision["kind"] != "action" or not decision["argv"]:
        return {**decision, "status": "not-runnable"}
    summary = "این دستور اجرا شود؟\n" + " ".join(decision["argv"][1:])
    if not notifier.approval("farsi_run", summary, {"argv": decision["argv"]}):
        return {**decision, "status": "denied"}
    if runner is None:
        completed = subprocess.run(decision["argv"], env=child_env(),
                                   creationflags=CREATE_NO_WINDOW)
        code = completed.returncode
    else:
        code = int(runner(decision["argv"]))
    feedback(db, decision["route"], text, code == 0)
    return {**decision, "status": "ok" if code == 0 else "error", "exit_code": code}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="farsi.intent", description="لایهٔ قصد")
    sub = parser.add_subparsers(dest="command", required=True)
    ask = sub.add_parser("say", help="تشخیص قصد یک متن")
    ask.add_argument("text")
    ask.add_argument("--learn", action="store_true")
    go = sub.add_parser("run", help="تشخیص و اجرا با تأیید")
    go.add_argument("text")
    teach_parser = sub.add_parser("teach", help="افزودن نمونهٔ جدید")
    teach_parser.add_argument("route")
    teach_parser.add_argument("text")
    fb = sub.add_parser("feedback", help="ثبت بازخورد")
    fb.add_argument("route")
    fb.add_argument("text")
    fb.add_argument("--no", action="store_true")
    sub.add_parser("routes", help="فهرست مسیرها و نمونه‌ها")
    args = parser.parse_args(argv)

    db = prepare()
    udb = usage.connect()
    try:
        if args.command == "routes":
            for route in routes(db):
                print(f"{route['key']:20} {route['kind']:12} {route['title']} "
                      f"(وزن {route['weight']:.2f})")
                for example in route["examples"]:
                    print(f"    · {example['text']}  [{example['origin']}]")
            return 0
        if args.command == "teach":
            print(teach(db, args.route, args.text))
            return 0
        if args.command == "feedback":
            print("وزن جدید:", feedback(db, args.route, args.text, not args.no))
            return 0
        if args.command == "say":
            decision = classify(args.text, db, udb, learn=args.learn)
            print(f"کلاس: {decision['kind']} | مسیر: {decision['route'] or '-'} "
                  f"| امتیاز: {decision['score']}")
            if decision["example"]:
                print("نمونهٔ نزدیک:", decision["example"])
            if decision["argv"]:
                print("دستور:", " ".join(decision["argv"][1:]))
            if decision["note"]:
                print("نکته:", decision["note"])
            return 0
        from agent.notify import Notifier
        report = run(args.text, db, udb, Notifier(interactive=True))
        print("وضعیت:", report["status"], "| کلاس:", report["kind"])
        if report["note"]:
            print("نکته:", report["note"])
        return 0 if report["status"] in ("ok", "not-runnable") else 1
    except (AgentError, OSError, ValueError) as exc:
        print("توقف:", exc)
        return 1
    finally:
        udb.close()
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())