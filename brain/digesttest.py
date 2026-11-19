"""Offline test of the knowledge digest and its bounded prompt block."""
from __future__ import annotations

import shutil

from agent.config import CODE_ROOT, get_logger

from . import capability, digest, knowledge

log = get_logger("digesttest")

SRC_JSON = ("import json\n"
            "def load_cfg(p):\n"
            "    with open(p, encoding='utf-8') as f:\n"
            "        return json.load(f)\n")
SRC_RE = ("import re\n"
          "def clean(text):\n"
          "    return re.split(' ', str(text))\n")
CARD = {"key": "k.test0001", "title": "الگوی آموخته‌شده: demo",
        "goals": ["برعکس", "k.test0001"],
        "inputs": [{"name": "text", "type": "str"}],
        "outputs": [{"name": "result", "type": "str"}],
        "packages": [], "effects": [], "context": [], "exclude": [],
        "priority": 20, "origin": "knowledge:test0001",
        "body": "def cap_k_demo(text):\n    return str(text)[::-1]\n"}
TOPICS = ["json", "csv", "عبارت باقاعده (re)"]


def run() -> int:
    results: list[tuple[str, bool, str]] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        results.append((name, ok, detail))
        print(("  ✓ " if ok else "  ! ") + name + (f" — {detail}" if detail else ""))

    print("تست آفلاین چکیدهٔ دانش")
    root = CODE_ROOT / "brain_digest_test"
    shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True, exist_ok=True)
    kdb = knowledge.connect(root / "knowledge.db")
    empty = knowledge.connect(root / "empty.db")
    db = capability.connect(root / "caps.db")
    try:
        signature = knowledge.learn_source(kdb, "python", "t.py", SRC_JSON)[0]
        knowledge.record_outcome(kdb, signature, "ok", "seed")
        knowledge.learn_source(kdb, "python", "u.py", SRC_RE)
        capability.seed(db)
        capability.upsert(db, CARD)

        covered = {item["tag"]: item for item in digest.coverage(kdb)}
        check("پوشش ساخته شد", bool(covered), str(sorted(covered)[:5]))
        check("شمار تأیید درست بود",
              covered.get("json", {}).get("verified") == 1,
              str(covered.get("json")))
        weak_tags = [item["tag"] for item in digest.weak(kdb)]
        check("برچسب تأییدنشده ضعیف شمرده شد", "regex" in weak_tags, str(weak_tags))
        apis = [item["api"] for item in digest.top_apis(kdb)]
        check("نام‌های API شمرده شدند", "json" in apis or "re" in apis, str(apis))

        plan = digest.curriculum(TOPICS, {"json": {}})
        check("خوانده/نخوانده درست شمرده شد",
              plan["read"] == 1 and plan["total"] == 3, str(plan))
        check("موضوع بعدی اولین نخوانده بود", plan["next"] == "csv", plan["next"])

        info = digest.build(db, kdb, topics=TOPICS, done={"json": {}})
        check("شمار کارت‌ها درست بود",
              info["learned"] == 1 and info["named"] == 1,
              f"{info['cards']}/{info['learned']}/{info['named']}")

        text = digest.render(info)
        check("بلوک به فارسی و شامل پوشش بود", "json" in text and "پوشش" in text)
        check("موضوع پیشنهادی در بلوک آمد", "csv" in text, info["next"])
        short = digest.render(info, max_chars=120)
        check("طول بلوک کراندار ماند", len(short) <= 120, str(len(short)))

        blank = digest.render(digest.build(db, empty, topics=[], done={}))
        check("دانش خالی هم بدون کرش خلاصه داد", bool(blank), str(len(blank)))
    finally:
        empty.close()
        kdb.close()
        db.close()
        shutil.rmtree(root, ignore_errors=True)

    failed = [name for name, ok, _ in results if not ok]
    print("\nنتیجه:", "همه تست‌ها پاس شد" if not failed else f"{len(failed)} تست شکست خورد")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(run())