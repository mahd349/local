"""Offline test of the knowledge -> capability bridge."""
from __future__ import annotations

import shutil

from agent.config import CODE_ROOT, get_logger

from . import bridge, capability, knowledge, probe

log = get_logger("bridgetest")

SHOUT = "def shout(text):\n    return text.upper()\n"
LENGTH = "def length(text):\n    return len(text)\n"
COUNTER = "def counter(n):\n    return n + 1\n"
NOOP = "def noop(text):\n    print(text)\n"
BROKEN = "def broken(text):\n    return missing_name\n"


def run() -> int:
    results: list[tuple[str, bool, str]] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        results.append((name, ok, detail))
        print(("  ✓ " if ok else "  ! ") + name + (f" — {detail}" if detail else ""))

    print("تست آفلاین پل دانش به قابلیت")
    root = CODE_ROOT / "agent_bridge_test"
    shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True, exist_ok=True)
    kdb_path = root / "knowledge_test.db"
    caps_path = root / "caps_test.db"
    kdb = knowledge.connect(kdb_path)
    db = capability.connect(caps_path)
    try:
        for index, source in enumerate((SHOUT, LENGTH, COUNTER, NOOP, BROKEN), start=1):
            for signature in knowledge.learn_source(kdb, "python", f"t{index}.py", source):
                knowledge.record_outcome(kdb, signature, "ok", "seed")
        capability.seed(db)
        check("دانش بذر آماده شد", knowledge.stats(kdb)["units"] >= 5,
              str(knowledge.stats(kdb)))

        rows = bridge.suggest(kdb, limit=20)
        names = {row["name"] for row in rows}
        check("واحد سالم در پیش‌نمایش آمد", "shout" in names, str(sorted(names)))
        check("ورودی عددی رد شد", "counter" not in names)
        check("تابع بدون خروجی رد شد", "noop" not in names)

        report = bridge.promote(db, kdb, limit=10, budget=probe.Budget(20, 60))
        check("حداقل یک کارت ساخته شد", report["promoted"] >= 1, str(report))

        cards = {card["key"]: card for card in capability.all_caps(db)}
        shout_key = next((k for k, c in cards.items() if c["title"].endswith("shout")), "")
        check("کارت در پایگاه قابلیت‌ها نشست", bool(shout_key), shout_key)
        check("کارت تأییدشده علامت خورد",
              bool(shout_key) and cards[shout_key]["verified"] == 1)
        check("نوع ورودی کارت معتبر است",
              bool(shout_key) and all(item["type"] in bridge.SAMPLES
                                      for item in cards[shout_key]["inputs"]))
        check("تابع خراب ارتقا نگرفت",
              not any(c["title"].endswith("broken") for c in cards.values()))
        check("واژگان نوع جدید اضافه شد",
              any(out["type"] == "int" for c in cards.values() for out in c["outputs"]))

        before = len(cards)
        bridge.promote(db, kdb, limit=10, budget=probe.Budget(20, 60))
        check("اجرای دوباره کارت تکراری نساخت",
              len(capability.all_caps(db)) == before, str(before))

        module = root / "ctx.py"
        module.write_text(
            "import re\n"
            "_CLEAN = re.compile(r'[^a-z]+')\n"
            "def slug(text):\n"
            "    return _CLEAN.sub('-', str(text).lower())\n",
            encoding="utf-8")
        body = module.read_text(encoding="utf-8")
        ctx = bridge.context_of(body)
        check("زمینهٔ ماژول شامل import و ثابت شد",
              "import re" in ctx and "_CLEAN" in ctx, ctx.replace("\n", " | ")[:70])
        for sig in knowledge.learn_source(kdb, "python", str(module), body):
            knowledge.record_outcome(kdb, sig, "ok", "seed")
        bridge.promote(db, kdb, limit=10, budget=probe.Budget(20, 60))
        check("الگوی وابسته به ثابت ماژول ارتقا گرفت",
              any(c["title"].endswith("slug") for c in capability.all_caps(db)))

        info = bridge.stats(db)
        check("آمار کارت‌های آموخته‌شده درست است",
              info["learned"] >= report["promoted"] and info["learned"] == info["verified"],
              str(info))
    finally:
        db.close()
        kdb.close()
        shutil.rmtree(root, ignore_errors=True)

    failed = [name for name, ok, _ in results if not ok]
    print("\nنتیجه:", "همه تست‌ها پاس شد" if not failed else f"{len(failed)} تست شکست خورد")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(run())