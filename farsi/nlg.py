"""Local reply writing: retrieve templates, fill typed slots, rank candidates.

Nothing is invented. A template is emitted only when every slot holds a real
value of the right type, and a factual answer requires a stored fact. The
n-gram model ranks candidates for fluency; it never generates words.
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import time
from pathlib import Path

from agent.config import STATE_DIR, assert_writable, ensure_dirs, get_logger
from agent.errors import AgentError

from . import usage
from .text import normalize

log = get_logger("farsi.nlg")

NLG_DB = STATE_DIR / "farsi_nlg.db"
MIN_TERMS_FOR_FLUENCY = 200
_SLOT = re.compile(r"\{([a-z_][a-z0-9_]*)\}")
_PATHISH = re.compile(r"(?:[A-Za-z]:[\\/]|^/|\\\\|\.)")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS templates(
key TEXT PRIMARY KEY, intent TEXT, text TEXT, slots TEXT,
weight REAL DEFAULT 0.5, uses INTEGER DEFAULT 0, successes INTEGER DEFAULT 0,
origin TEXT, created_ts REAL, updated_ts REAL);
CREATE INDEX IF NOT EXISTS idx_templates_intent ON templates(intent, weight DESC);
CREATE TABLE IF NOT EXISTS facts(
topic TEXT PRIMARY KEY, text TEXT, origin TEXT, ts REAL);
CREATE TABLE IF NOT EXISTS replies(
id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, intent TEXT, key TEXT,
text TEXT, accepted INTEGER);
"""

SEEDS = [
    {"key": "act.plan", "intent": "*", "weight": 0.5,
     "text": "نقشه آماده است: {command}",
     "slots": [{"name": "command", "type": "text"}]},
    {"key": "act.confirm", "intent": "*", "weight": 0.5,
     "text": "باشه، «{title}» را انجام می‌دهم.",
     "slots": [{"name": "title", "type": "text"}]},
    {"key": "act.needs_input", "intent": "*", "weight": 0.6,
     "text": "برای «{title}» یک ورودی لازم دارم: {missing}",
     "slots": [{"name": "title", "type": "text"}, {"name": "missing", "type": "text"}]},
    {"key": "feel.listen", "intent": "talk.feeling", "weight": 0.5,
     "text": "متوجه‌ام. اگر بخواهی می‌توانم کاری انجام دهم، یا فقط ادامه بده.",
     "slots": []},
    {"key": "feel.offer", "intent": "talk.feeling", "weight": 0.5,
     "text": "شنیدم. دوست داری چیزی را تغییر بدهم یا فقط می‌خواهی بگویی؟",
     "slots": []},
    {"key": "greet.hi", "intent": "talk.greeting", "weight": 0.5,
     "text": "سلام. چه کاری برایت انجام دهم؟", "slots": []},
    {"key": "greet.ready", "intent": "talk.greeting", "weight": 0.5,
     "text": "درود. آماده‌ام؛ بگو چه می‌خواهی.", "slots": []},
    {"key": "explain.fact", "intent": "talk.explain", "weight": 0.8,
     "text": "{fact}", "slots": [{"name": "fact", "type": "text"}]},
    {"key": "explain.unknown", "intent": "talk.explain", "weight": 0.6,
     "text": "برای این پرسش هنوز چیزی یاد نگرفته‌ام. اگر بخواهی می‌توانم بپرسم و یاد بگیرم.",
     "slots": []},
    {"key": "unknown.ask", "intent": "unknown", "weight": 0.6,
     "text": "مطمئن نیستم منظورت کدام کار است. نزدیک‌ترین حدس «{title}» بود؛ درست است؟",
     "slots": [{"name": "title", "type": "text"}]},
    {"key": "talk.plain", "intent": "conversation", "weight": 0.5,
     "text": "این را گفت‌وگو فهمیدم، نه دستور. کاری هست انجام دهم؟", "slots": []},
]


def connect(path: str | Path | None = None) -> sqlite3.Connection:
    ensure_dirs()
    target = assert_writable(Path(path) if path else NLG_DB)
    target.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(target))
    db.row_factory = sqlite3.Row
    db.executescript(_SCHEMA)
    return db


def _row(row: sqlite3.Row) -> dict:
    item = dict(row)
    item["slots"] = json.loads(item["slots"] or "[]")
    return item


def upsert(db: sqlite3.Connection, raw: dict) -> str:
    if not raw.get("key") or not raw.get("text"):
        raise AgentError("قالب به key و text نیاز دارد")
    now = time.time()
    db.execute(
        "INSERT INTO templates(key,intent,text,slots,weight,origin,created_ts,updated_ts) "
        "VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(key) DO UPDATE SET text=excluded.text, "
        "slots=excluded.slots, intent=excluded.intent, updated_ts=excluded.updated_ts",
        (str(raw["key"]), str(raw.get("intent") or "*"), str(raw["text"]),
         json.dumps(raw.get("slots") or [], ensure_ascii=False),
         float(raw.get("weight", 0.5)), str(raw.get("origin", "seed")), now, now),
    )
    db.commit()
    return str(raw["key"])


def seed(db: sqlite3.Connection) -> int:
    for item in SEEDS:
        upsert(db, item)
    return len(SEEDS)


def prepare(path: str | Path | None = None) -> sqlite3.Connection:
    db = connect(path)
    if not db.execute("SELECT COUNT(*) AS n FROM templates").fetchone()["n"]:
        seed(db)
    return db


def templates_for(db: sqlite3.Connection, intents) -> list[dict]:
    """Specific intents first; the wildcard bucket is always the last resort."""
    chain = [intents] if isinstance(intents, str) else list(intents)
    ordered = [name for name in chain if name]
    if "*" not in ordered:
        ordered.append("*")
    marks = ",".join("?" for _ in ordered)
    rows = db.execute(
        f"SELECT * FROM templates WHERE intent IN ({marks})", ordered
    ).fetchall()
    rank = {name: index for index, name in enumerate(ordered)}
    out = []
    for row in rows:
        item = _row(row)
        item["rank"] = rank.get(item["intent"], len(ordered))
        out.append(item)
    out.sort(key=lambda item: (item["rank"], -float(item["weight"]), item["key"]))
    return out


def _valid(kind: str, value: str) -> bool:
    text = str(value).strip()
    if not text:
        return False
    if kind == "path":
        return bool(_PATHISH.search(text))
    if kind == "url":
        return text.startswith(("http://", "https://"))
    return True


def fill(template: dict, context: dict) -> tuple[str | None, list[str]]:
    """A template with an unfilled or mistyped slot is rejected, never emitted."""
    kinds = {slot["name"]: slot.get("type", "text") for slot in template["slots"]}
    missing: list[str] = []

    def replace(match):
        name = match.group(1)
        value = context.get(name)
        if value is None or not _valid(kinds.get(name, "text"), value):
            missing.append(name)
            return match.group(0)
        return str(value)

    text = _SLOT.sub(replace, template["text"])
    if missing or "{" in text:
        return None, missing
    return " ".join(text.split()), []


def remember_fact(db: sqlite3.Connection, topic: str, text: str,
                  origin: str = "user") -> str:
    key = normalize(topic)
    db.execute(
        "INSERT INTO facts(topic,text,origin,ts) VALUES(?,?,?,?) "
        "ON CONFLICT(topic) DO UPDATE SET text=excluded.text, origin=excluded.origin, "
        "ts=excluded.ts", (key, str(text).strip(), origin, time.time()),
    )
    db.commit()
    return key


def find_fact(db: sqlite3.Connection, topic: str) -> dict | None:
    row = db.execute("SELECT * FROM facts WHERE topic=?", (normalize(topic),)).fetchone()
    return dict(row) if row else None


def candidates(db: sqlite3.Connection, udb, intents, context: dict) -> list[dict]:
    terms = usage.stats(udb)["terms"] if udb is not None else 0
    fluent = terms >= MIN_TERMS_FOR_FLUENCY
    out: list[dict] = []
    for template in templates_for(db, intents):
        text, missing = fill(template, context)
        if text is None:
            continue
        weight = float(template["weight"])
        score = weight
        if fluent:
            probability = 2.0 ** usage.score(udb, text)
            score = weight * (0.5 + 0.5 * max(0.0, min(1.0, probability)))
        out.append({"key": template["key"], "text": text, "weight": weight,
                    "score": round(score, 4), "uses": int(template["uses"]),
                    "rank": int(template["rank"]), "missing": missing})
    out.sort(key=lambda item: (item["rank"], -item["score"], item["uses"], item["key"]))
    return out


def reply(db: sqlite3.Connection, udb, decision: dict, extra: dict | None = None,
          vary: bool = False) -> dict:
    kind = decision.get("kind") or "conversation"
    route = decision.get("route") or ""
    context = {"title": decision.get("title") or "", "text": decision.get("text") or ""}
    context.update(decision.get("values") or {})
    if decision.get("argv"):
        context["command"] = " ".join(str(part) for part in decision["argv"][1:])
    if decision.get("missing"):
        context["missing"] = ", ".join(decision["missing"])
    context.update(extra or {})

    # An unsure intent must never reach an action template.
    if kind == "unknown":
        chain = ["unknown"]
    elif kind == "action":
        chain = [route, "action"]
    else:
        chain = [route, "conversation"]
    label = next((name for name in chain if name), "conversation")

    notes: list[str] = []
    grounded = False
    if route == "talk.explain":
        fact = find_fact(db, decision.get("text") or "")
        if fact:
            context["fact"] = fact["text"]
            grounded = True
            notes.append("پاسخ از واقعیت ذخیره‌شده")
        else:
            notes.append("واقعیتی ذخیره نشده؛ حدس زده نشد")

    pool = candidates(db, udb, chain, context)
    if not pool and kind != "unknown":
        pool = candidates(db, udb, ["conversation"], context)
        if pool:
            label = "conversation"
            notes.append("قالب اختصاصی نبود؛ به گفت‌وگوی ساده برگشت")
    if not pool:
        return {"text": "", "key": "", "intent": label, "confidence": "low",
                "grounded": False, "notes": ["هیچ قالبی با ورودی موجود پر نشد"]}

    chosen = pool[0]
    if vary:
        top = pool[:3]
        chosen = min(top, key=lambda item: (item["uses"], item["rank"],
                                            -item["score"], item["key"]))

    terms = usage.stats(udb)["terms"] if udb is not None else 0
    if terms < MIN_TERMS_FOR_FLUENCY:
        notes.append("پیکره کوچک است؛ رتبه‌بندی روانی خاموش")

    if chosen["weight"] >= 0.6 and (route != "talk.explain" or grounded):
        confidence = "high"
    elif chosen["weight"] >= 0.4:
        confidence = "medium"
    else:
        confidence = "low"
    if kind == "unknown" and confidence == "high":
        confidence = "medium"

    now = time.time()
    db.execute("UPDATE templates SET uses=uses+1, updated_ts=? WHERE key=?",
               (now, chosen["key"]))
    db.execute("INSERT INTO replies(ts,intent,key,text) VALUES(?,?,?,?)",
               (now, label, chosen["key"], chosen["text"]))
    db.commit()
    return {"text": chosen["text"], "key": chosen["key"], "intent": label,
            "confidence": confidence, "grounded": grounded, "notes": notes,
            "alternatives": [item["text"] for item in pool[1:3]]}


def teach(db: sqlite3.Connection, intent: str, text: str,
          slots: list | None = None, key: str | None = None) -> str:
    body = str(text).strip()
    if not body:
        raise AgentError("متن قالب خالی است")
    names = sorted(set(_SLOT.findall(body)))
    declared = slots or [{"name": name, "type": "text"} for name in names]
    missing = [name for name in names if name not in {s["name"] for s in declared}]
    if missing:
        raise AgentError("نوع این جایگاه‌ها مشخص نشده: " + ", ".join(missing))
    return upsert(db, {"key": key or f"user.{int(time.time())}", "intent": intent,
                       "text": body, "slots": declared, "weight": 0.6, "origin": "user"})


def feedback(db: sqlite3.Connection, key: str, accepted: bool) -> float:
    row = db.execute("SELECT weight FROM templates WHERE key=?", (key,)).fetchone()
    if row is None:
        raise AgentError(f"قالب ناشناس: {key}")
    weight = min(1.0, float(row["weight"]) + 0.1) if accepted else \
        max(0.0, float(row["weight"]) - 0.15)
    db.execute(
        "UPDATE templates SET weight=?, successes=successes+?, updated_ts=? WHERE key=?",
        (weight, 1 if accepted else 0, time.time(), key),
    )
    db.execute("UPDATE replies SET accepted=? WHERE id=(SELECT MAX(id) FROM replies WHERE key=?)",
               (1 if accepted else 0, key))
    db.commit()
    return weight


def forget(db: sqlite3.Connection, key: str) -> bool:
    cursor = db.execute("DELETE FROM templates WHERE key=?", (key,))
    db.commit()
    return cursor.rowcount > 0


def stats(db: sqlite3.Connection) -> dict:
    row = db.execute("SELECT COUNT(*) AS n, AVG(weight) AS w, SUM(uses) AS u "
                     "FROM templates").fetchone()
    facts = db.execute("SELECT COUNT(*) AS n FROM facts").fetchone()["n"]
    return {"templates": row["n"] or 0, "avg_weight": round(row["w"] or 0.0, 3),
            "uses": row["u"] or 0, "facts": facts}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="farsi.nlg", description="نوشتن پاسخ محلی")
    sub = parser.add_subparsers(dest="command", required=True)
    say = sub.add_parser("say", help="فهم درخواست و نوشتن پاسخ")
    say.add_argument("text")
    say.add_argument("--vary", action="store_true")
    teach_parser = sub.add_parser("teach", help="افزودن قالب پاسخ")
    teach_parser.add_argument("intent")
    teach_parser.add_argument("text")
    fact = sub.add_parser("remember", help="ذخیرهٔ یک واقعیت")
    fact.add_argument("topic")
    fact.add_argument("text")
    fb = sub.add_parser("feedback", help="ثبت بازخورد آخرین پاسخ")
    fb.add_argument("key")
    fb.add_argument("--no", action="store_true")
    sub.add_parser("list", help="فهرست قالب‌ها")
    sub.add_parser("stats", help="آمار")
    args = parser.parse_args(argv)

    db = prepare()
    udb = usage.connect()
    try:
        if args.command == "say":
            from . import intent as intent_module
            idb = intent_module.prepare()
            try:
                decision = intent_module.classify(args.text, idb, udb)
            finally:
                idb.close()
            answer = reply(db, udb, decision, vary=args.vary)
            print(f"قصد: {decision['kind']} / {decision['route'] or '-'}")
            print(f"پاسخ: {answer['text']}")
            print(f"قالب: {answer['key']} | اعتماد: {answer['confidence']} "
                  f"| مستند: {answer['grounded']}")
            for note in answer["notes"]:
                print("  نکته:", note)
            if decision["argv"]:
                print("دستور:", " ".join(decision["argv"][1:]))
            return 0
        if args.command == "teach":
            print("قالب ثبت شد:", teach(db, args.intent, args.text))
            return 0
        if args.command == "remember":
            print("واقعیت ثبت شد:", remember_fact(db, args.topic, args.text))
            return 0
        if args.command == "feedback":
            print("وزن جدید:", feedback(db, args.key, not args.no))
            return 0
        if args.command == "list":
            for row in db.execute("SELECT * FROM templates ORDER BY intent, key").fetchall():
                item = _row(row)
                names = ", ".join(s["name"] + ":" + s.get("type", "text")
                                  for s in item["slots"]) or "-"
                print(f"{item['key']:20} {item['intent']:16} w={item['weight']:.2f} "
                      f"uses={item['uses']} [{names}]")
                print("   ", item["text"][:90])
            return 0
        print(stats(db))
        return 0
    except (AgentError, OSError, ValueError) as exc:
        print("توقف:", exc)
        return 1
    finally:
        udb.close()
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())