"""Turn a request into a typed plan: goals -> capabilities -> wiring.

Forward matching picks the goals, backward chaining fills prerequisites, and
type compatibility decides whether two blocks can be joined. No local model is
used, so the same request always produces the same plan.
"""
from __future__ import annotations

import argparse
import json
from dataclasses import dataclass, field

from . import capability
from agent.config import REPORT_DIR, assert_writable, get_logger
from agent.errors import AgentError

log = get_logger("planner")

_ARABIC = {"ي": "ی", "ك": "ک", "ة": "ه", "أ": "ا", "إ": "ا", "ؤ": "و"}
_INVISIBLE = ("\u200c", "\u200d", "\u200e", "\u200f")
MAX_DEPTH = 3


def normalize(text: str) -> str:
    out = []
    for char in str(text):
        char = _ARABIC.get(char, char)
        if char in _INVISIBLE:
            continue
        out.append(char)
    return "".join(out).lower()


def compatible(want: str, have: str) -> bool:
    """Only text-like types are interchangeable; path and url stay distinct."""
    want, have = str(want), str(have)
    if want == "any" or have == "any" or want == have:
        return True
    text_like = {"str", "text"}
    return want in text_like and have in text_like


@dataclass
class Plan:
    request: str
    steps: list = field(default_factory=list)
    packages: list = field(default_factory=list)
    gaps: list = field(default_factory=list)
    notes: list = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(self.steps) and not self.gaps

    def as_dict(self) -> dict:
        return {
            "request": self.request, "ok": self.ok,
            "steps": [{"key": s["key"], "title": s["title"], "args": s["args"],
                       "outputs": s["outputs"]} for s in self.steps],
            "packages": self.packages, "gaps": self.gaps, "notes": self.notes,
        }


def match_goals(caps: list[dict], request: str) -> list[str]:
    text = normalize(request)
    hits = []
    for card in caps:
        if card["context"] and not any(normalize(w) in text for w in card["context"]):
            continue
        if card["exclude"] and any(normalize(w) in text for w in card["exclude"]):
            continue
        best = None
        for keyword in card["goals"]:
            position = text.find(normalize(keyword))
            if position >= 0 and (best is None or position < best):
                best = position
        if best is not None:
            hits.append((best, card["priority"], -card["weight"], card["key"]))
    hits.sort()
    ordered, seen = [], set()
    for _, _, _, key in hits:
        if key not in seen:
            seen.add(key)
            ordered.append(key)
    return ordered


def build_plan(request: str, caps: list[dict], params: dict | None = None) -> Plan:
    params = dict(params or {})
    plan = Plan(request=request)
    by_key = {card["key"]: card for card in caps}
    env: list[tuple[str, str]] = []
    counter = [0]

    def newvar() -> str:
        counter[0] += 1
        return f"v{counter[0]}"

    def lookup(kind: str) -> str | None:
        for name, seen_type in reversed(env):
            if compatible(kind, seen_type):
                return name
        return None

    def snapshot() -> tuple:
        return len(plan.steps), len(env), len(plan.packages)

    def rollback(mark: tuple) -> None:
        del plan.steps[mark[0]:]
        del env[mark[1]:]
        del plan.packages[mark[2]:]

    def emit(card: dict, depth: int) -> str | None:
        mark = snapshot()
        args = []
        for slot in card["inputs"]:
            name = slot["name"]
            if name in params:
                args.append(repr(str(params[name])))
                continue
            ref = lookup(slot["type"])
            if ref is None and slot.get("default") is not None:
                ref = repr(str(slot["default"]))
            if ref is None:
                ref = produce(slot["type"], depth - 1, card["key"])
            if ref is None:
                rollback(mark)
                return None
            args.append(ref)
        var = newvar() if card["outputs"] else ""
        plan.steps.append({
            "key": card["key"], "title": card["title"], "body": card["body"],
            "args": args, "outputs": [var] if var else [],
            "effects": card["effects"],
        })
        if var:
            env.append((var, card["outputs"][0]["type"]))
        for package in card["packages"]:
            if package not in plan.packages:
                plan.packages.append(package)
        # "" means a void capability succeeded; None means it failed.
        return var

    def produce(kind: str, depth: int, skip_key: str = "") -> str | None:
        if depth <= 0:
            return None
        providers = [c for c in caps
                     if c["key"] != skip_key
                     and any(compatible(kind, o["type"]) for o in c["outputs"])]
        providers.sort(key=lambda c: (c["priority"], -c["verified"], -c["weight"]))
        for card in providers:
            if card["packages"]:
                continue  # never pull a new dependency in silently
            result = emit(card, depth)
            if result:
                plan.notes.append(f"پیش‌نیاز «{kind}» با {card['key']} تأمین شد")
                return result
        return None


    keys = match_goals(caps, request)
    if not keys:
        plan.gaps.append("no-goal")
        return plan
    for key in keys:
        if any(step["key"] == key for step in plan.steps):
            continue
        if emit(by_key[key], MAX_DEPTH) is None:
            plan.gaps.append(key)
    if plan.packages:
        plan.notes.append("نیاز به نصب: " + ", ".join(plan.packages))
    return plan


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agent.planner", description="نقشهٔ ساخت برنامه")
    parser.add_argument("request")
    parser.add_argument("--param", action="append", default=[], dest="params")
    parser.add_argument("--build", action="store_true", help="کد را هم بساز")
    parser.add_argument("--out", default=None)
    args = parser.parse_args(argv)

    params = {}
    for item in args.params:
        if "=" not in item:
            raise AgentError("--param باید name=value باشد")
        key, value = item.split("=", 1)
        params[key] = value

    db = capability.prepare()
    try:
        caps = capability.all_caps(db)
        plan = build_plan(args.request, caps, params)
        for index, step in enumerate(plan.steps, start=1):
            print(f"{index}. {step['key']:16} {step['title']}")
        for note in plan.notes:
            print("  •", note)
        if plan.gaps:
            print("کمبود:", ", ".join(plan.gaps))
        if not args.build:
            return 0 if plan.ok else 1
        from .assembler import build
        report = build(plan, out_path=args.out)
        print("ساخته شد:", report["path"] if report["written"] else "(نوشته نشد)")
        print("بررسی:", report["check"], report["detail"])
        REPORT_DIR.mkdir(parents=True, exist_ok=True)
        target = assert_writable(REPORT_DIR / "last_plan.json")
        target.write_text(json.dumps(plan.as_dict(), ensure_ascii=False, indent=2),
                          encoding="utf-8")
        return 0 if plan.ok else 1
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())