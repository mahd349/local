"""Offline test of the usage engine: counts, phrases, neighbours, n-gram ranking."""
from __future__ import annotations

import shutil

from agent.config import CODE_ROOT, get_logger

from . import corpus, usage

log = get_logger("farsi.usagetest")

SKY_ONE = "آسمان آبی است چون پراکندگی نور در جو رخ می دهد"
SKY_TWO = "پراکندگی نور باعث آبی دیده شدن آسمان می شود"
SEA = "دریا آبی است و آب نور را بازتاب می دهد"


def run() -> int:
    results: list[tuple[str, bool, str]] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        results.append((name, ok, detail))
        print(("  ✓ " if ok else "  ! ") + name + (f" — {detail}" if detail else ""))

    print("تست آفلاین موتور هم‌نشینی")
    root = CODE_ROOT / "farsi_usage_test"
    shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True, exist_ok=True)
    cdb = corpus.connect(root / "corpus.db")
    db = usage.connect(root / "usage.db")
    was_on = corpus.learning_enabled()
    try:
        corpus.set_learning(True)
        for _ in range(3):
            corpus.add_text(cdb, SKY_ONE)
            corpus.add_text(cdb, SKY_TWO)
        corpus.add_text(cdb, SEA)
        # Duplicate texts are deduplicated by the corpus, so add distinct variants.
        for index in range(3):
            corpus.add_text(cdb, SKY_ONE + f" نمونهٔ {index}")
            corpus.add_text(cdb, SKY_TWO + f" نمونهٔ {index}")

        report = usage.build(db, cdb)
        info = usage.stats(db)
        check("آمار واژه‌ها ساخته شد", info["terms"] > 0, str(info["terms"]))
        check("هم‌نشینی ثبت شد", info["pairs"] > 0, str(info["pairs"]))
        check("جدول n-gram ساخته شد", info["grams"] > 0, str(info["grams"]))

        found = {item["phrase"] for item in usage.discover_phrases(db, min_count=3.0)}
        check("عبارت پرتکرار کشف شد", "پراکندگی نور" in found,
              str(sorted(found)[:4]))

        near = usage.neighbors(db, "پراکندگی", limit=3)
        check("همسایهٔ نزدیک درست بود",
              any(item["term"] == "نور" for item in near),
              str([item["term"] for item in near]))
        check("همسایه‌های آسمان خالی نبود",
              bool(usage.neighbors(db, "آسمان", limit=5)))

        options = usage.next_options(db, "پراکندگی")
        check("ادامهٔ محتمل درست بود",
              bool(options) and options[0]["token"] == "نور",
              str([item["token"] for item in options]))

        seen = usage.score(db, "پراکندگی نور")
        unseen = usage.score(db, "نور پراکندگی")
        check("ترتیب دیده‌شده امتیاز بالاتری گرفت", seen > unseen,
              f"{seen} > {unseen}")

        again = usage.build(db, cdb)
        check("اجرای دوباره چیزی اضافه نکرد", again["docs"] == 0, str(again))

        corpus.add_text(cdb, "نور در آب هم شکست می خورد")
        fresh = usage.build(db, cdb)
        check("متن جدید پردازش شد", fresh["docs"] == 1, str(fresh))

        check("اعتماد واژهٔ دیده‌شده مثبت بود",
              usage.confidence(db, "پراکندگی") > 0,
              str(usage.confidence(db, "پراکندگی")))
        check("اعتماد واژهٔ نادیده صفر بود",
              usage.confidence(db, "واژه‌ای‌که‌نیامده") == 0.0)

        corpus.set_learning(False)
        blocked = usage.build(db, cdb)
        check("کلید خاموش جلوی ساخت را گرفت",
              blocked.get("skipped") == "learn-off", str(blocked))
        corpus.set_learning(True)

        pruned = usage.prune(db, min_pair=999.0)
        check("هرس جدول‌ها را سبک کرد", pruned["pairs_after"] == 0, str(pruned))
    finally:
        db.close()
        cdb.close()
        corpus.set_learning(was_on)
        shutil.rmtree(root, ignore_errors=True)

    failed = [name for name, ok, _ in results if not ok]
    print("\nنتیجه:", "همه تست‌ها پاس شد" if not failed else f"{len(failed)} تست شکست خورد")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(run())