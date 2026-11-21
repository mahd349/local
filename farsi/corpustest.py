"""Offline test of the Persian corpus layer."""
from __future__ import annotations

import json
import shutil

from agent.config import CODE_ROOT, get_logger

from . import corpus
from .text import normalize, persian_ratio, quality, sentences, tokens

log = get_logger("farsi.corpustest")

SENTENCE = "سلام، حال تو چطوره؟ امروز کتاب خواندم."
NOISE = "aaaaaaa"
SECRET = "password: SuperSecret123"


def run() -> int:
    results: list[tuple[str, bool, str]] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        results.append((name, ok, detail))
        print(("  ✓ " if ok else "  ! ") + name + (f" — {detail}" if detail else ""))

    print("تست آفلاین پیکرهٔ فارسی")
    root = CODE_ROOT / "farsi_test"
    shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True, exist_ok=True)
    db = corpus.connect(root / "corpus_test.db")
    was_on = corpus.learning_enabled()
    try:
        check("حروف عربی یکسان‌سازی شدند",
              normalize("كتابي ة") == "کتابی ه", normalize("كتابي ة"))
        check("رقم‌ها یکسان شدند", normalize("۱۲۳٤٥") == "12345", normalize("۱۲۳٤٥"))
        check("نیم‌فاصله در کلید حذف شد", normalize("می\u200cرود") == "میرود")
        check("واژهٔ نیم‌فاصله‌دار یک توکن ماند",
              tokens("می‌رود") == ["میرود"], str(tokens("می‌رود")))
        check("جمله‌ها جدا شدند", len(sentences(SENTENCE)) == 2, str(sentences(SENTENCE)))
        check("نسبت فارسی تشخیص داده شد", persian_ratio(SENTENCE) > 0.8,
              str(round(persian_ratio(SENTENCE), 2)))
        check("کیفیت متن نویزی پایین‌تر بود",
              quality(NOISE) < quality(SENTENCE), f"{quality(NOISE)} < {quality(SENTENCE)}")

        corpus.set_learning(False)
        blocked = corpus.add_text(db, SENTENCE)
        check("کلید خاموش جلوی ذخیره را گرفت",
              not blocked["stored"] and blocked["reason"] == "learn-off", str(blocked))

        corpus.set_learning(True)
        first = corpus.add_text(db, SENTENCE, source="user", speaker="me")
        check("پس از روشن شدن ذخیره شد", first["stored"], str(first))
        again = corpus.add_text(db, SENTENCE)
        check("متن تکراری دوباره ذخیره نشد",
              not again["stored"] and again["reason"] == "duplicate", str(again))

        masked = corpus.add_text(db, SECRET)
        row = corpus.recent(db, 1)[0]
        check("رمز پیش از ذخیره پوشانده شد",
              masked["stored"] and "SuperSecret123" not in row["raw"], row["raw"][:40])

        archive = root / "chat.json"
        archive.write_text(json.dumps({"messages": [
            {"role": "user", "content": "یک سوال دربارهٔ پایتون دارم"},
            {"role": "assistant", "content": "بپرس، گوش می‌دهم."},
        ]}, ensure_ascii=False), encoding="utf-8")
        imported = corpus.add_json(db, archive)
        check("پیام‌های JSON خوانده شدند", imported["stored"] == 2, str(imported))

        info = corpus.stats(db)
        check("آمار تعداد و توکن را داد",
              info["docs"] == 4 and info["tokens"] > 0, str(info))
        check("آخرین متن‌ها برگشتند", len(corpus.recent(db, 2)) == 2)
    finally:
        db.close()
        corpus.set_learning(was_on)
        shutil.rmtree(root, ignore_errors=True)

    failed = [name for name, ok, _ in results if not ok]
    print("\nنتیجه:", "همه تست‌ها پاس شد" if not failed else f"{len(failed)} تست شکست خورد")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(run())