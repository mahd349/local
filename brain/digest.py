"""A bounded digest of what the brain knows, for the lesson prompt.

The whole knowledge store cannot be typed into a chat, so this renders a short
summary: covered areas with evidence counts, weak areas, and unread curriculum
topics. Coverage comes from objective tags and API names, never from guessed
meanings, and the rendered block is length-capped.
"""
from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from pathlib import Path

from agent.config import STATE_DIR, get_logger
from agent.errors import AgentError

from . import capability, knowledge

log = get_logger("digest")

LESSON_DIR = STATE_DIR / "lessons"
MAX_CHARS = 1200
_PERSIAN = re.compile(r"[\u0600-\u06FF]")


def _load(path: Path, fallback):
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            pass
    return fallback


def _buckets(kdb) -> list[dict]:
    buckets: dict[str, dict] = {}
    for row in kdb.execute("SELECT tags, verified, weight FROM units"):
        try:
            tags = json.loads(row["tags"] or "[]")
        except ValueError:
            continue
        for tag in tags:
            item = buckets.setdefault(str(tag), {"tag": str(tag), "units": 0,
                                                 "verified": 0, "mass": 0.0})
            item["units"] += 1
            item["verified"] += int(row["verified"] or 0)
            item["mass"] += float(row["weight"] or 0.0)
    out = []
    for item in buckets.values():
        item["avg_weight"] = round(item.pop("mass") / max(1, item["units"]), 3)
        out.append(item)
    out.sort(key=lambda entry: (-entry["units"], entry["tag"]))
    return out


def coverage(kdb, limit: int = 12) -> list[dict]:
    return _buckets(kdb)[:limit]


def weak(kdb, limit: int = 8) -> list[dict]:
    return [item for item in _buckets(kdb) if item["verified"] == 0][:limit]


def top_apis(kdb, limit: int = 10) -> list[dict]:
    counts: Counter = Counter()
    for row in kdb.execute("SELECT apis FROM units"):
        try:
            apis = json.loads(row["apis"] or "[]")
        except ValueError:
            continue
        for api in apis:
            counts[str(api).split(".")[0]] += 1
    return [{"api": name, "n": number} for name, number in counts.most_common(limit)]


def curriculum(topics=None, done=None, limit: int = 8) -> dict:
    if topics is None:
        topics = _load(LESSON_DIR / "topics.json", [])
    if done is None:
        done = _load(LESSON_DIR / "progress.json", {})
    listed = [str(item) for item in (topics or [])]
    finished = done if isinstance(done, dict) else {}
    unread = [topic for topic in listed if topic not in finished]
    return {"total": len(listed), "read": len(listed) - len(unread),
            "unread": unread[:limit], "next": unread[0] if unread else ""}


def build(db, kdb, *, topics=None, done=None, limit: int = 12) -> dict:
    cards = capability.all_caps(db)
    learned = [card for card in cards if card["key"].startswith("k.")]
    named = [card for card in learned
             if any(_PERSIAN.search(str(goal)) for goal in card["goals"])]
    plan = curriculum(topics, done)
    info = {"units": knowledge.stats(kdb), "cards": len(cards),
            "learned": len(learned), "named": len(named),
            "coverage": coverage(kdb, limit), "weak": weak(kdb, 8),
            "apis": top_apis(kdb, 10), "curriculum": plan}
    info["next"] = plan["next"] or (info["weak"][0]["tag"] if info["weak"] else "")
    return info


def render(info: dict, max_chars: int = MAX_CHARS) -> str:
    stats = info["units"]
    lines = ["وضعیت دانش من (خلاصه):",
             f"- الگوهای دیده‌شده: {stats['units']} | تأییدشده با اجرا: {stats['verified']}",
             f"- کارت‌های قابل استفاده: {info['cards']} (آموخته‌شده: {info['learned']})"]
    if info["coverage"]:
        parts = [f"{item['tag']} {item['verified']}/{item['units']}"
                 for item in info["coverage"][:10]]
        lines.append("- پوشش (تأیید/کل): " + "، ".join(parts))
    if info["weak"]:
        lines.append("- ضعیف یا تأییدنشده: "
                     + "، ".join(item["tag"] for item in info["weak"][:8]))
    plan = info["curriculum"]
    if plan["total"]:
        lines.append(f"- برنامهٔ درسی: {plan['read']} از {plan['total']} خوانده شده")
    if plan["unread"]:
        lines.append("- خوانده‌نشده: " + "، ".join(plan["unread"][:6]))
    if info["next"]:
        lines.append(f"- موضوع پیشنهادی بعدی: {info['next']}")
    lines.append("فقط چیزی را آموزش بده که در فهرست بالا نیست یا ضعیف است؛"
                 " موضوعات پوشش‌داده‌شده را تکرار نکن.")
    return "\n".join(lines)[:max_chars]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="brain.digest", description="چکیدهٔ دانش")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("show", help="آمار پوشش")
    block = sub.add_parser("render", help="متنی که به چت فرستاده می‌شود")
    block.add_argument("--max-chars", type=int, default=MAX_CHARS)
    sub.add_parser("next", help="موضوع پیشنهادی بعدی")
    args = parser.parse_args(argv)

    db = capability.prepare()
    kdb = knowledge.connect()
    try:
        info = build(db, kdb)
        if args.command == "render":
            print(render(info, args.max_chars))
            return 0
        if args.command == "next":
            print(info["next"] or "(چیزی پیشنهاد نشد)")
            return 0
        print("الگوها:", info["units"], "| کارت‌ها:", info["cards"],
              "| آموخته‌شده:", info["learned"], "| نام‌دار:", info["named"])
        for item in info["coverage"]:
            print(f"  {item['tag']:16} {item['verified']}/{item['units']} "
                  f"وزن {item['avg_weight']}")
        print("ضعیف:", "، ".join(item["tag"] for item in info["weak"]) or "-")
        print("برنامهٔ درسی:", info["curriculum"])
        return 0
    except (AgentError, OSError, ValueError) as exc:
        print("توقف:", exc)
        return 1
    finally:
        kdb.close()
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())