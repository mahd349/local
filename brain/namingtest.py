"""Offline test of evidence-based Persian naming for learned cards."""
from __future__ import annotations

import shutil

from agent.config import CODE_ROOT, get_logger

from . import capability, knowledge, naming

log = get_logger("namingtest")

UNIT = ('def demo(text):\n'
        '    """برعکس کردن متن ورودی."""\n'
        '    return str(text)[::-1]\n')


def run() -> int:
    results: list[tuple[str, bool, str]] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        results.append((name, ok, detail))
        print(("  ✓ " if ok else "  ! ") + name + (f" — {detail}" if detail else ""))

    print("تست آفلاین نام‌گذاری فارسی")
    root = CODE_ROOT / "brain_naming_test"
    shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True, exist_ok=True)
    db = capability.connect(root / "caps.db")
    kdb = knowledge.connect(root / "knowledge.db")
    try:
        check("جدول کارت‌ها پیدا شد", bool(naming._caps_table(db)), naming._caps_table(db))
        capability.seed(db)
        seed_before = next(c for c in capability.all_caps(db) if c["key"] == "text.literal")

        signature = knowledge.learn_source(kdb, "python", "t.py", UNIT)[0]
        knowledge.record_outcome(kdb, signature, "ok", "seed")
        key = "k." + signature
        capability.upsert(db, {
            "key": key, "title": "الگوی آموخته‌شده: demo", "goals": [key],
            "inputs": [{"name": "text", "type": "str"}],
            "outputs": [{"name": "result", "type": "str"}],
            "packages": [], "effects": [], "context": [], "exclude": [],
            "priority": 20, "origin": "knowledge:" + signature,
            "body": "def cap_k_demo(text):\n    return str(text)[::-1]\n",
        })
        check("کارت بی‌نام در صف آمد", key in [c["key"] for c in naming.pending(db)])

        words = naming.evidence(knowledge.get(kdb, signature))
        check("شاهد فارسی استخراج شد", "برعکس" in words, str(words))

        report = naming.auto(db, kdb, limit=10)
        check("نام‌گذاری خودکار انجام شد", report["named"] == 1, str(report))
        check("کارت از صف خارج شد", key not in [c["key"] for c in naming.pending(db)])

        card = next(c for c in capability.all_caps(db) if c["key"] == key)
        check("توکن ماشینی هم نگه داشته شد", key in card["goals"], str(card["goals"]))
        sidecar = naming.CAPS_DIR / f"naming_{key.replace('.', '_')}.json"
        check("فایل کنارِ کارت نوشته شد", sidecar.exists(), str(sidecar.name))

        merged = naming.set_goals(db, key, ["وارونه", "وارونه", "ab"])
        check("نام دستی نشست و تکراری/کوتاه حذف شد",
              "وارونه" in merged and "ab" not in merged
              and merged.count("وارونه") == 1, str(merged))

        seed_after = next(c for c in capability.all_caps(db) if c["key"] == "text.literal")
        check("کارت پایه دست‌نخورده ماند", seed_after["goals"] == seed_before["goals"])

        info = naming.stats(db)
        check("آمار نام‌گذاری درست بود",
              info["learned"] == 1 and info["named"] == 1 and info["unnamed"] == 0,
              str(info))
    finally:
        kdb.close()
        db.close()
        shutil.rmtree(root, ignore_errors=True)

    failed = [name for name, ok, _ in results if not ok]
    print("\nنتیجه:", "همه تست‌ها پاس شد" if not failed else f"{len(failed)} تست شکست خورد")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(run())