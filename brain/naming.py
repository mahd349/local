"""Persian names for learned capability cards, derived only from evidence.

A learned card carries a machine token as its goal, so a Persian request never
reaches it. This module adds Persian goals taken from the unit's own Persian
text and its nearby context. Nothing is invented: with no evidence the card is
reported as unnamed so the user can name it.
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from agent.config import STATE_DIR, assert_writable, get_logger
from agent.errors import AgentError

from . import capability, knowledge

log = get_logger("naming")

CAPS_DIR = STATE_DIR / "caps"
ZWNJ = "\u200c"
_PERSIAN = re.compile(r"[\u0600-\u06FF]")
_WORD = re.compile(r"[\u0621-\u06CC\u200c]+")
# Noise filter only: these carry no intent, so they make poor goals.
STOPWORDS = {
    "را", "به", "از", "در", "با", "که", "این", "آن", "برای", "می", "شود",
    "است", "های", "یک", "هم", "تا", "اگر", "ولی", "نه", "بله", "شد", "کرد",
}
_CARD_FIELDS = ("key", "title", "goals", "inputs", "outputs", "packages",
                "effects", "context", "exclude", "priority", "origin", "body")


def _caps_table(db) -> str:
    """Discover the card table instead of assuming its name."""
    for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'"):
        name = row["name"]
        columns = {r["name"] for r in db.execute(f"PRAGMA table_info({name})")}
        if {"key", "goals"} <= columns:
            return name
    raise AgentError("جدول کارت‌ها پیدا نشد")


def _word(raw: str) -> str:
    return str(raw or "").replace(ZWNJ, "").strip()


def has_persian(goals: list) -> bool:
    return any(_PERSIAN.search(str(goal)) for goal in goals)


def pending(db) -> list[dict]:
    return [card for card in capability.all_caps(db)
            if card["key"].startswith("k.") and not has_persian(card["goals"])]


def evidence(unit: dict) -> list[str]:
    """Persian words present in the unit and its immediate file context."""
    text = str(unit.get("source") or "")
    origin = Path(str(unit.get("origin") or ""))
    if origin.is_absolute() and origin.suffix.lower() == ".py" and origin.exists():
        try:
            body = origin.read_text(encoding="utf-8-sig", errors="replace")
        except OSError:
            body = ""
        anchor = text.strip()[:60]
        index = body.find(anchor) if anchor else -1
        if index >= 0:
            text = body[max(0, index - 400): index + len(text) + 200]
    words: list[str] = []
    for match in _WORD.finditer(text):
        word = _word(match.group(0))
        if len(word) >= 3 and word not in STOPWORDS and word not in words:
            words.append(word)
    return words


def _sidecar(card: dict, goals: list[str], origin: str) -> Path:
    """A card file in the caps folder keeps the name across later promotions."""
    payload = {field: card[field] for field in _CARD_FIELDS if field in card}
    payload["goals"] = goals
    payload["origin"] = f"naming:{origin}"
    CAPS_DIR.mkdir(parents=True, exist_ok=True)
    target = assert_writable(CAPS_DIR / f"naming_{card['key'].replace('.', '_')}.json")
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return target


def set_goals(db, key: str, goals: list, *, origin: str = "user") -> list[str]:
    card = next((c for c in capability.all_caps(db) if c["key"] == key), None)
    if card is None:
        raise AgentError(f"کارت پیدا نشد: {key}")
    cleaned: list[str] = []
    for goal in goals:
        word = _word(goal)
        if len(word) >= 3 and word not in cleaned:
            cleaned.append(word)
    if not cleaned:
        raise AgentError("هیچ هدف معتبری داده نشد (حداقل ۳ نویسه)")
    merged = list(dict.fromkeys(cleaned + list(card["goals"])))
    db.execute(f"UPDATE {_caps_table(db)} SET goals=? WHERE key=?",
               (json.dumps(merged, ensure_ascii=False), key))
    db.commit()
    _sidecar(card, merged, origin)
    return merged


def auto(db, kdb, *, limit: int = 20) -> dict:
    stats = {"seen": 0, "named": 0, "no_evidence": 0}
    for card in pending(db):
        if stats["seen"] >= limit:
            break
        stats["seen"] += 1
        unit = knowledge.get(kdb, card["key"][2:])
        words = evidence(unit) if unit else []
        if not words:
            stats["no_evidence"] += 1
            continue
        set_goals(db, card["key"], words[:6], origin="evidence")
        stats["named"] += 1
    return stats


def stats(db) -> dict:
    cards = capability.all_caps(db)
    learned = [c for c in cards if c["key"].startswith("k.")]
    named = [c for c in learned if has_persian(c["goals"])]
    return {"cards": len(cards), "learned": len(learned), "named": len(named),
            "unnamed": len(learned) - len(named)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="brain.naming", description="نام فارسی کارت‌ها")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("pending", help="کارت‌های بی‌نام فارسی")
    run = sub.add_parser("auto", help="نام‌گذاری از شاهدِ متن")
    run.add_argument("--limit", type=int, default=20)
    manual = sub.add_parser("set", help="نام‌گذاری دستی")
    manual.add_argument("key")
    manual.add_argument("goals", nargs="+")
    sub.add_parser("stats", help="آمار نام‌گذاری")
    args = parser.parse_args(argv)

    db = capability.prepare()
    kdb = knowledge.connect()
    try:
        if args.command == "pending":
            rows = pending(db)
            for card in rows:
                print(f"{card['key']}  {card['title']}")
            print(f"\n{len(rows)} کارت بی‌نام فارسی")
            return 0
        if args.command == "auto":
            print(auto(db, kdb, limit=args.limit))
            print(stats(db))
            return 0
        if args.command == "set":
            print("هدف‌ها:", set_goals(db, args.key, args.goals))
            return 0
        print(stats(db))
        return 0
    except (AgentError, OSError, ValueError) as exc:
        print("توقف:", exc)
        return 1
    finally:
        kdb.close()
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())